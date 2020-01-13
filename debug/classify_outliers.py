import numpy as np
from bayes_gain_screens.outlier_detection import filter_tec_dir
import glob, os
from bayes_gain_screens.datapack import DataPack
from bayes_gain_screens.misc import voronoi_finite_polygons_2d, get_coordinates
import matplotlib
matplotlib.use('tkagg')
import pylab as plt
from scipy.spatial import Voronoi, cKDTree
from scipy.optimize import linprog
from astropy.io import fits
from astropy.wcs import WCS
import tensorflow as tf
import networkx as nx
from graph_nets.utils_np import networkxs_to_graphs_tuple
from sklearn.model_selection import StratifiedShuffleSplit

def flatten(f):
    """ Flatten a fits file so that it becomes a 2D image. Return new header and data """

    naxis = f[0].header['NAXIS']
    if naxis < 2:
        raise ValueError('Cannot make map from this')
    if naxis == 2:
        return fits.PrimaryHDU(header=f[0].header, data=f[0].data)

    w = WCS(f[0].header)
    wn = WCS(naxis=2)

    wn.wcs.crpix[0] = w.wcs.crpix[0]
    wn.wcs.crpix[1] = w.wcs.crpix[1]
    wn.wcs.cdelt = w.wcs.cdelt[0:2]
    wn.wcs.crval = w.wcs.crval[0:2]
    wn.wcs.ctype[0] = w.wcs.ctype[0]
    wn.wcs.ctype[1] = w.wcs.ctype[1]

    header = wn.to_header()
    header["NAXIS"] = 2
    copy = ('EQUINOX', 'EPOCH', 'BMAJ', 'BMIN', 'BPA', 'RESTFRQ', 'TELESCOP', 'OBSERVER')
    for k in copy:
        r = f[0].header.get(k)
        if r is not None:
            header[k] = r

    slice = []
    for i in range(naxis, 0, -1):
        if i <= 2:
            slice.append(np.s_[:], )
        else:
            slice.append(0)

    hdu = fits.PrimaryHDU(header=header, data=f[0].data[tuple(slice)])
    return hdu


def in_hull(points, x):
    n_points = len(points)
    n_dim = len(x)
    c = np.zeros(n_points)
    A = np.r_[points.T,np.ones((1,n_points))]
    b = np.r_[x, np.ones(1)]
    lp = linprog(c, A_eq=A, b_eq=b)
    return lp.success

def build_training_dataset(label_file, ref_image, datapack, K=3):
    """

    :param label_file:
    :param datapack:
    :return:
    """

    with fits.open(ref_image) as f:
        hdu = flatten(f)
        data = hdu.data
        wcs = WCS(hdu.header)


    dp = DataPack(datapack, readonly=True)

    dp.current_solset = 'directionally_referenced'
    dp.select(pol=slice(0, 1, 1))
    tec, axes = dp.tec
    _, Nd, Na, Nt = tec.shape
    tec_uncert, _ = dp.weights_tec
    _, directions = dp.get_directions(axes['dir'])
    directions = np.stack([directions.ra.deg, directions.dec.deg], axis=1)
    directions = wcs.wcs_world2pix(directions, 0)

    __, nn_idx = cKDTree(directions).query(directions, k=K + 1)

    dp = DataPack(datapack, readonly=True)

    dp.current_solset = 'directionally_referenced'
    dp.select(pol=slice(0, 1, 1))
    tec, axes = dp.tec
    _, Nd, Na, Nt = tec.shape
    tec_uncert, _ = dp.weights_tec

    if label_file is not None:
        #Nd, Na, Nt
        human_flags = np.load(label_file)
        #Nd*Na,Nt, 1
        labels = human_flags.reshape((Nd*Na,Nt, 1))
        mask = human_flags != -1

    # tec = np.pad(tec,[(0,0),(0,0), (0,0), (window_size, window_size)],mode='reflect')
    # tec_uncert = np.pad(tec_uncert,[(0,0),(0,0), (0,0), (window_size, window_size)],mode='reflect')

    inputs = []
    for d in range(Nd):
        #K+1, Na, Nt, 2
        input = np.stack([tec[0,nn_idx[d,:],:,:]/10., np.log(tec_uncert[0,nn_idx[d,:],:,:])], axis=-1)
        #Na, Nt, (K+1)*2
        input = np.transpose(input, (1,2,0,3)).reshape((Na,Nt, (K+1)*2))
        inputs.append(input)

    #Nd*Na,Nt, (K+1)*2
    inputs = np.concatenate(inputs, axis=0)
    if label_file is not None:
        return [inputs, labels, mask]
    return [inputs]

def get_output_bias(label_files):
    num_pos = 0
    num_neg = 0
    for label_file in label_files:
        human_flags = np.load(label_file)
        num_pos += np.sum(human_flags==1)
        num_neg += np.sum(human_flags==0)
    pos_weight = num_neg/num_pos
    bias = np.log(num_pos) - np.log(num_neg)
    return bias, pos_weight



class Classifier(object):
    def __init__(self, L=4, K=3, n_features = 16, batch_size=16, graph=None, output_bias=0., pos_weight = 1.):
        if graph is None:
            graph = tf.Graph()
        self.graph = graph
        self.K = K
        N = (K + 1) * 2
        self.L = L
        self.n_features = n_features
        with self.graph.as_default():
            self.label_files_pl = tf.placeholder(tf.string, shape=[None], name='label_files')
            self.datapacks_pl = tf.placeholder(tf.string, shape=[None], name='datapacks')
            self.ref_images_pl = tf.placeholder(tf.string, shape=[None], name='ref_images')
            self.shard_idx = tf.placeholder(tf.int32, shape=[])

            train_dataset = tf.data.Dataset.from_tensor_slices([self.label_files_pl, self.ref_images_pl, self.datapacks_pl])
            train_dataset = train_dataset.flat_map(self._build_training_dataset, num_parallel_calls=5)
            train_dataset = train_dataset.shard(2,self.shard_idx).shuffle(1000).map(self._augment)\
                .batch(batch_size=batch_size, drop_remainder=True)

            iterator_tensor = train_dataset.make_initializable_iterator()
            self.train_init = iterator_tensor.initializer
            self.train_inputs, self.train_labels, self.train_mask = iterator_tensor.get_next()

            train_outputs = self.build_model(self.train_inputs, output_bias=output_bias)
            labels_ext = tf.broadcast_to(self.train_labels, tf.shape(train_outputs))
            self.pred_probs = tf.nn.sigmoid(train_outputs)
            self.conf_mat = tf.math.confusion_matrix(tf.reshape(self.train_labels, (-1,)),
                                                     tf.reshape(self.pred_probs, (-1,)),
                                                     weights=tf.reshape(self.train_mask, (-1,)),
                                                     num_classes=2, dtype=tf.float32)
            loss = tf.nn.weighted_cross_entropy_with_logits(labels=labels_ext, logits=train_outputs, pos_weight=pos_weight)
            self.loss = tf.reduce_mean(loss * self.train_mask)
            self.global_step = tf.Variable(0, trainable=False)
            self.opt = tf.train.AdamOptimizer().minimize(self.loss, global_step=self.global_step)

            eval_dataset = tf.data.Dataset.from_tensor_slices(self.label_files_pl)
            eval_dataset = eval_dataset.flat_map(self._build_eval_dataset, num_parallel_calls=5) \
                .batch(batch_size=batch_size, drop_remainder=False)

            iterator_tensor = eval_dataset.make_initializable_iterator()
            self.eval_init = iterator_tensor.initializer
            self.eval_inputs = iterator_tensor.get_next()

            eval_outputs = self.build_model(self.eval_inputs, output_bias=output_bias)
            self.eval_pred_probs = tf.nn.sigmoid(eval_outputs)

    def _build_training_dataset(self, label_file, ref_image, datapack):
        return tf.py_function(lambda label_file, ref_image, datapack:
                       build_training_dataset(label_file.numpy(), ref_image.numpy(), datapack.numpy(), self.K),
                       [label_file, ref_image, datapack],
                       [tf.float32, tf.float32, tf.float32]
                       )

    def _build_eval_dataset(self, ref_image, datapack):
        return tf.py_function(lambda ref_image, datapack:
                       build_training_dataset(None, ref_image.numpy(), datapack.numpy(), self.K),
                       [ref_image, datapack],
                       [tf.float32]
                       )

    def train_model(self, label_files, ref_images, datapacks, epochs=10, working_dir='./training'):
        os.makedirs(working_dir, exist_ok=True)
        with tf.Session(graph=self.graph) as sess:
            saver = tf.train.Saver()
            sess.run(tf.initialize_variables(tf.trainable_variables()))
            saver.restore(sess, working_dir)
            for epoch in range(epochs):
                sess.run(self.train_init,
                         {self.label_files_pl: label_files,
                          self.ref_images_pl: ref_images,
                          self.datapacks_pl: datapacks,
                          self.shard_idx: 0})
                conf_mat = np.zeros((2,2))
                epoch_loss = 0
                while True:
                    try:
                        _, loss, _conf_mat, global_step = sess.run([self.opt, self.loss, self.conf_mat, self.global_step])
                        conf_mat = conf_mat + _conf_mat
                        epoch_loss += loss
                        if global_step % 100 == 0:
                            with np.printoptions(precision=2):
                                print("TRAIN: Iter {:04d} loss {}\n\tBatch Conf mat: {}".format(global_step, loss, _conf_mat))
                    except tf.errors.OutOfRangeError:
                        break
                print("TRAIN: Epoch loss: {}".format(epoch_loss))
                print("TRAIN: Epoch Conf mat: {}".format(conf_mat))
                sess.run(self.train_init,
                         {self.label_files_pl: label_files,
                          self.ref_images_pl: ref_images,
                          self.datapacks_pl: datapacks,
                          self.shard_idx: 1})
                conf_mat = np.zeros((2, 2))
                epoch_loss = 0
                while True:
                    try:
                        loss, _conf_mat, global_step = sess.run(
                            [self.loss, self.conf_mat, self.global_step])
                        conf_mat = conf_mat + _conf_mat
                        epoch_loss += loss
                        if global_step % 100 == 0:
                            with np.printoptions(precision=2):
                                print("TEST: Iter {:04d} loss {}\n\tBatch Conf mat: {}".format(global_step, loss,
                                                                                                _conf_mat))
                    except tf.errors.OutOfRangeError:
                        break
                print("TEST: Epoch loss: {}".format(epoch_loss))
                print("TEST: Epoch Conf mat: {}".format(conf_mat))
                print('Saving...')
                saver.save(sess, working_dir, global_step=self.global_step)

    def eval_model(self, ref_images, datapacks, working_dir='./training'):
        with tf.Session(graph=self.graph) as sess:
            saver = tf.train.Saver()
            sess.run(tf.initialize_variables(tf.trainable_variables()))
            saver.restore(sess, working_dir)
            sess.run(self.eval_init,
                     {self.ref_images_pl: ref_images,
                      self.datapacks_pl: datapacks})
            predictions = []
            while True:
                try:
                    probs = sess.run(self.eval_pred_probs)
                    winner = np.median(probs, axis=0)
                    predictions.append(winner)
                except tf.errors.OutOfRangeError:
                    break
            predictions = np.concatenate(predictions, axis=0)



    def build_model(self, inputs, output_bias=0.):
        with tf.variable_scope('classifier', reuse=tf.AUTO_REUSE):
            num = 0
            features = tf.layers.conv1d(inputs, self.n_features, [1], strides=1, padding='same', activation=None,name='conv_{:02d}'.format(num))
            num += 1
            outputs = []
            for s in [1, 2, 3, 4]:
                for d in [1, 2, 3, 4]:
                    if s > 1 and d > 1:
                        continue
                    for pool in [tf.layers.average_pooling1d, tf.layers.max_pooling1d]:
                        h = [features]
                        for i in range(self.L):
                            u = tf.layers.conv1d(h[i], self.n_features, [3], s, padding='same', dilation_rate=d, activation=tf.nn.relu,
                                                 use_bias=True,name='conv_{:02d}'.format(num))
                            num += 1
                            u = pool(u, pool_size=3, strides=1, padding='same')
                            h.append(u - h[i])
                    outputs.append(h[-1])
            # S, Nd*Na, Nt
            outputs = tf.stack([tf.layers.conv1d(o, 1, [1], padding='same',name='conv_{:02d}'.format(num), use_bias=False) for o in outputs], axis=0)
            output_bias = tf.Variable(output_bias, dtype=tf.float32, trainable=True)
            outputs += output_bias
            num += 1
            return outputs

    def _augment(self, inputs, labels, mask, crop_size):
        sizes = [inputs.shape.as_list[-1,labels.shape.as_list[-1],mask.shape.as_list[-1]]]
        c = np.cumsum(sizes)
        N = sum(sizes)
        large = tf.concat([inputs, labels, mask], axis=-1)
        large = tf.image.random_flip_up_down(
            tf.image.random_crop(large, (crop_size, N))[..., None])[..., 0]
        inputs, labels, mask = large[..., :c[0]], large[..., c[0]:c[1]], large[..., c[1]:c[2]]
        return [inputs, labels, mask]



def click_through(datapack, ref_image, working_dir, reset = False):
    with fits.open(ref_image) as f:
        hdu = flatten(f)
        data = hdu.data
        wcs = WCS(hdu.header)
    window = 20



    linked_datapack = os.path.join(working_dir,os.path.basename(os.path.abspath(datapack)))
    if os.path.islink(linked_datapack):
        os.unlink(linked_datapack)
    print("Linking {} -> {}".format(os.path.abspath(datapack), linked_datapack))
    os.symlink(os.path.abspath(datapack), linked_datapack)

    linked_ref_image = linked_datapack.replace('.h5','.ref_image.fits')
    if os.path.islink(linked_ref_image):
        os.unlink(linked_ref_image)
    print("Linking {} -> {}".format(os.path.abspath(ref_image), linked_ref_image))
    os.symlink(os.path.abspath(ref_image), linked_ref_image)

    save_file = linked_datapack.replace('.h5','.labels.npy')

    dp = DataPack(datapack, readonly=True)

    dp.current_solset = 'directionally_referenced'
    dp.select(pol=slice(0, 1, 1))
    tec, axes = dp.tec
    _, Nd, Na, Nt = tec.shape
    tec_uncert, _ = dp.weights_tec
    _, directions = dp.get_directions(axes['dir'])
    directions = np.stack([directions.ra.deg, directions.dec.deg], axis=1)
    directions = wcs.wcs_world2pix(directions, 0)
    _, times = dp.get_times(axes['time'])
    times = times.mjd*86400.
    times -= times[0]
    times /= 3600.


    window_time = times[window]

    xmin = directions[:, 0].min()
    xmax = directions[:, 0].max()
    ymin = directions[:, 1].min()
    ymax = directions[:, 1].max()

    radius = xmax - xmin

    ref_dir = directions[0:1, :]

    _, guess_flags = filter_tec_dir(tec[0,...], directions, init_y_uncert=None, min_res=8.)
    # guess_flags = np.ones([Nd, Na, Nt], np.bool)
    if os.path.isfile(save_file) and not reset:
        human_flags = np.load(save_file)
    else:
        human_flags = -1*np.ones([Nd, Na, Nt], np.int32)

    # compute Voronoi tesselation
    vor = Voronoi(directions)

    point_to_region_map = vor.point_region
    region_to_point_map = np.argsort(vor.point_region)
    print(point_to_region_map)
    print(region_to_point_map)
    __, nn_idx = cKDTree(directions).query(directions, k=4)

    regions, vertices = voronoi_finite_polygons_2d(vor, radius)

    fig = plt.figure(constrained_layout=False, figsize=(12, 12))

    gs = fig.add_gridspec(3,2)
    time_ax = fig.add_subplot(gs[0, :])
    time_ax.set_xlabel('time [hours]')
    time_ax.set_ylabel('DDTEC [mTECU]')

    time_plots = [time_ax.plot( np.arange(window*2),  0.*np.arange(window*2), c='black')[0] for _ in range(4)]

    dir_ax = fig.add_subplot(gs[1:, :], projection=wcs)
    dir_ax.coords[0].set_axislabel('Right Ascension (J2000)')
    dir_ax.coords[1].set_axislabel('Declination (J2000)')
    dir_ax.coords.grid(True, color='grey', ls='solid')
    polygons = []
    cmap = plt.cm.get_cmap('PuOr')
    norm = plt.Normalize(-1., 1.)
    colors = np.zeros(Nd)
    # colorize
    for color, region in zip(colors, regions):
        if np.size(color) == 1:
            if norm is None:
                color = cmap(color)
            else:
                color = cmap(norm(color))
        polygon = vertices[region]
        polygons.append(dir_ax.fill(*zip(*polygon), color=color, alpha=1., linewidth=4, edgecolor='black')[0])

    dir_ax.scatter(ref_dir[:, 0], ref_dir[:, 1], marker='*', color='black', zorder=19)

    # plt.plot(points[:,0], points[:,1], 'ko')
    dir_ax.set_xlim(vor.min_bound[0] - 0.1*radius, vor.max_bound[0] + 0.1*radius)
    dir_ax.set_ylim(vor.min_bound[1] - 0.1*radius, vor.max_bound[1] + 0.1*radius)

    def onkeyrelease(event):
        print('Pressed {} ({}, {})'.format(event.key, event.xdata, event.ydata))
        if event.key == 'n':
            print("Saving... going to next.")
            np.save(save_file, human_flags)
            next_loc = min(loc[0]+1, len(order))
            load_data(next_loc)
        if event.key == 'b':
            print("Saving... going to back.")
            np.save(save_file, human_flags)
            next_loc = max(loc[0]-1, 0)
            load_data(next_loc)
        if event.key == 's':
            print("Saving...")
            np.save(save_file, human_flags)

    def onclick(event):
        _, a, t, norm = loc
        print('%s click: button=%d, x=%d, y=%d, xdata=%f, ydata=%f' %
              ('double' if event.dblclick else 'single', event.button,
               event.x, event.y, event.xdata, event.ydata))
        for i,region in enumerate(regions):
            point = i
            if in_hull(vertices[region], np.array([event.xdata, event.ydata])):
                print("In region {} (point {})".format(i, point))
                if event.button == 1:
                    print("Changing {}".format(human_flags[point,a,t]))
                    if human_flags[point,a,t] == -1 or human_flags[point,a,t] == 0:
                        human_flags[point, a, t] = 1
                        polygons[i].set_edgecolor('red')
                    elif human_flags[point, a, t] == 1:
                        human_flags[point, a, t] = 0
                        polygons[i].set_edgecolor('green')
                    polygons[i].set_zorder(11)
                    print("to {}".format(human_flags[point, a, t]))
                if event.button == 3:
                    start = max(0, t - window)
                    stop = min(Nt, t + window)
                    for n in range(4):
                        closest_idx = nn_idx[point, n]
                        time_plot = time_plots[n]
                        time_plot.set_data(times[start:stop], tec[0,closest_idx,a,start:stop])
                        if n == 0:
                            time_plot.set_color('black')
                        else:
                            time_plot.set_color(cmap(norm(tec[0,closest_idx, a, t])))
                    time_ax.set_xlim(times[t] - window_time, times[t] + window_time)
                    time_ax.set_ylim(tec[0,point,a,start:stop].min()-5., tec[0,point,a,start:stop].max()+5.)
                fig.canvas.draw()
                # break

    cid_click = fig.canvas.mpl_connect('button_press_event', onclick)
    cid_key = fig.canvas.mpl_connect('key_release_event', onkeyrelease)

    #Na, Nt

    search_first = np.where(np.any(guess_flags, axis=0))
    search_second = np.where(np.any(np.logical_not(guess_flags), axis=0))
    search = [list(sf)+list(ss) for sf, ss in zip(search_first, search_second)]
    order = list(np.random.choice(len(search_first[0]), len(search_first[0]), replace=False)) + \
            list(len(search_first[0])+np.random.choice(len(search_second[0]), len(search_second[0]), replace=False))
    loc = [0,0,0, plt.Normalize(-1.,1.)]

    def load_data(next_loc):
        loc[0] = next_loc
        o = order[next_loc]
        a = search[0][o]
        t = search[1][o]
        loc[1] = a
        loc[2] = t
        print("Looking at ant{:02d} and time {}".format(a, t))
        vmin, vmax = np.min(tec[0, :, a, t]), np.max(tec[0, :, a, t])
        vmin, vmax = min(vmin, -vmax), max(vmax, -vmin)
        norm = plt.Normalize(vmin, vmax)
        loc[3] = norm
        for i, p in enumerate(polygons):
            p.set_facecolor(cmap(norm(tec[0, i, a, t])))
            if guess_flags[i, a, t]:
                p.set_edgecolor('cyan')
                p.set_zorder(11)
            else:
                p.set_edgecolor('black')
                p.set_zorder(10)
        human_flags[:, a, t] = 0
        fig.canvas.draw()

    load_data(0)
    plt.show()
    return save_file

if __name__ == '__main__':
    # dp = '/net/nederrijn/data1/albert/screens/root/L562061/download_archive/L562061_DDS4_full_merged.h5'
    # ref_img = '/net/nederrijn/data1/albert/screens/root/L562061/download_archive/image_full_ampphase_di_m.NS.mask01.fits'
    # click_through(dp, ref_img)

    import os, glob
    working_dir = os.path.join(os.getcwd(), 'outlier_detection')
    datapacks = glob.glob('/home/albert/store/root_dense/L*/download_archive/L*_DDS4_full_merged.h5')
    ref_images = [os.path.join(os.path.dirname(f), 'image_full_ampphase_di_m.NS.app.restored.fits') for f in datapacks]
    label_files = []
    for dp, ref_img in zip(datapacks, ref_images):
        label_files.append(click_through(dp, ref_img, working_dir, reset=False))

    output_bias, pos_weight = get_output_bias(label_files)
    c = Classifier(L=4, K=3, n_features=16, batch_size=16, output_bias=output_bias, pos_weight=pos_weight)
    c.train_model(label_files, ref_images, datapacks, epochs=10, working_dir=os.path.join(working_dir, 'model'))
