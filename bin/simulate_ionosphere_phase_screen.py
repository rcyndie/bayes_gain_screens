import argparse
import sys
import logging
logger = logging.getLogger(__name__)


from bayes_gain_screens.tomographic_kernel import TomographicKernel
from bayes_gain_screens.utils import make_coord_array
from bayes_gain_screens.plotting import plot_vornoi_map
from bayes_gain_screens.frames import ENU
from h5parm import DataPack
from jaxns.gaussian_process.kernels import RBF
import jax.numpy as jnp
from jax.scipy.linalg import solve_triangular
from jax import jit, random, vmap
from h5parm.utils import create_empty_datapack
import astropy.units as au
import astropy.coordinates as ac
import pylab as plt
import numpy as np

ARRAYS = {'lofar': DataPack.lofar_array_hba}

def get_num_directions(avg_spacing, field_of_view_diameter):
    V = 2.*np.pi*(field_of_view_diameter/2.)**2
    pp = 0.5
    n = -V * np.log(1. - pp) / avg_spacing**2 / np.pi / 2.
    n = max(int(n), 50)
    return n

def compute_conditional_moments(kernel:TomographicKernel, X_new, X_old):
    f_K = jit(lambda X1, X2: kernel(X1, X2, bottom=300., width=50., l=4., sigma=1.))
    K_new_new = f_K(X_new, X_new)
    L_new = jnp.linalg.cholesky(K_new_new + 1e-6*jnp.eye(K_new_new.shape[0]))
    K_old_old = f_K(X_old, X_old)
    K_old_new = f_K(X_old, X_new)
    L = jnp.linalg.cholesky(K_old_old + 1e-6 * jnp.eye(K_old_old.shape[0]))
    JT = solve_triangular(L, K_old_new, lower=True)
    C = K_new_new - JT.T @ JT
    LC = jnp.linalg.cholesky(C + 1e-6*jnp.eye(C.shape[0]))
    # K_new_old @ (K_old_old)^-1 m(old)
    # K = L @ L^T
    # K^-1 = L^-T @ L^-1
    # (L^-T @ J.T)^T
    M = solve_triangular(L.T, JT, lower=False)
    return L_new, LC, M

def main(output_h5parm, ncpu, ra, dec,
         array_name, start_time, time_resolution, duration,
         field_of_view_diameter, avg_direction_spacing, east_wind, north_wind, time_block_size):
    Nd = get_num_directions(avg_direction_spacing, field_of_view_diameter)
    Nf = 1
    Nt = int(duration / time_resolution) + 1
    dp = create_empty_datapack(Nd, Nf, Nt, pols=None,
                          field_of_view_diameter=field_of_view_diameter,
                          start_time=start_time,
                          time_resolution=time_resolution,
                          min_freq=122.,
                          max_freq=166.,
                          array_file=ARRAYS[array_name],
                          phase_tracking=(ra, dec),
                          save_name=output_h5parm,
                          clobber=True)

    with dp:
        dp.current_solset = 'sol000'
        dp.select(pol=slice(0, 1, 1))
        axes = dp.axes_tec
        patch_names, directions = dp.get_directions(axes['dir'])
        antenna_labels, antennas = dp.get_antennas(axes['ant'])
        timestamps, times = dp.get_times(axes['time'])
    avg_time = times[len(times) // 2]
    antennas = ac.ITRS(*antennas.cartesian.xyz, obstime=avg_time)
    ref_ant = antennas[0]
    frame = ENU(obstime=avg_time, location=ref_ant.earth_location)
    antennas = antennas.transform_to(frame)
    ref_ant = antennas[0]
    x0 = ref_ant.cartesian.xyz.to(au.km).value
    directions = directions.transform_to(frame)
    t = times.mjd*86400.
    t -= t[0]
    dt = jnp.mean(jnp.diff(t))
    x = antennas.cartesian.xyz.to(au.km).value.T
    k = directions.cartesian.xyz.value.T
    Na = x.shape[0]
    Nd = k.shape[0]
    Nt = t.shape[0]

    #m(X_new) = K(X_new, X_old) @ (K(X_old, X_old))^{-1} m(X_old)
    #K(X_new, X_new) = K(X_new, X_new) - K(X_new, X_old) @ (K(X_old, X_old))^{-1} K(X_old, X_new)

    wind_vector = jnp.asarray([east_wind, north_wind, 0.])/1000.#km/s
    def wind_shift(coords):
        x = coords[0:3]
        k = coords[3:6]
        t = coords[6]
        return jnp.concatenate([x - wind_vector*t, k])

    X_new = make_coord_array(x, k, dt*jnp.arange(time_block_size)[:,None], flat=True)#N,7
    X_new = vmap(wind_shift)(X_new)#N,6
    X_old = make_coord_array(x, k, -dt * jnp.arange(1,3)[:, None], flat=True)  # N,7
    X_old = vmap(wind_shift)(X_old)  # N,6

    kernel = TomographicKernel(x0, x0, RBF(), S_marg=25)
    L_first, LC, M = jit(compute_conditional_moments, static_argnums=[0])(kernel, X_new, X_old)

    dtec = L_first @ random.normal(random.PRNGKey(24532), shape=(L_first.shape[0],1))
    dtec = dtec.reshape((Na, Nd, time_block_size))
    del L_first

    dtec_results = [dtec]
    for _ in range(Nt//time_block_size):
        last_result = dtec_results[-1]
        last_result = last_result[:, :, -2:].reshape((Na*Nd*2,1))
        mean = M @ last_result#M, 1
        next_result = mean + LC @ random.normal(random.PRNGKey(24532), shape=(LC.shape[0],1))
        next_result = next_result.reshape((Na, Nd, time_block_size))
        dtec_results.append(next_result)
        ax = plot_vornoi_map(k[:, 0:2], next_result[-1,:,-1])
        ax.set_xlabel(r"$k_{\rm east}$")
        ax.set_ylabel(r"$k_{\rm north}$")
        ax.set_xlim(-0.1, 0.1)
        ax.set_ylim(-0.1, 0.1)
        plt.show()

def debug_main():
    main(output_h5parm='test_datapack.h5',
         ncpu=1,
         ra=120.,
         dec=30.,
         array_name='lofar',
         start_time=None,
         time_resolution=30.,
         duration=60.,
         field_of_view_diameter=4.,
         avg_direction_spacing=6.,
         east_wind=120.,
         north_wind=0.,
         time_block_size=2)

def add_args(parser):
    parser.register("type", "bool", lambda v: v.lower() == "true")
    parser.add_argument('--output_h5parm', help='H5Parm file to file to place the simulated differential TEC',
                        default=None, type=str, required=True)
    parser.add_argument('--ra', help='RA in degrees in ICRS frame.',
                        default=None, type=float, required=True)
    parser.add_argument('--dec', help='DEC in degrees in ICRS frame.',
                        default=None, type=float, required=True)
    parser.add_argument('--array_name', help=f'Name of array, options are {sorted(list(ARRAYS.keys()))}.',
                        default=None, type=float, required=True)
    parser.add_argument('--start_time', help=f'Start time in modified Julian days (mjs/86400).',
                        default=None, type=float, required=True)
    parser.add_argument('--time_resolution', help=f'Temporal resolution in seconds.',
                        default=30., type=float, required=False)
    parser.add_argument('--time_resolution', help=f'Temporal duration in seconds.',
                        default=30., type=float, required=False)
    parser.add_argument('--field_of_view_diameter', help=f'Diameter of field of view in degrees.',
                        default=4., type=float, required=False)
    parser.add_argument('--avg_direction_spacing', help=f'Average spacing between directions in arcmin.',
                        default=6., type=float, required=False)
    parser.add_argument('--east_wind', help=f'Velocity of wind to the east at 100km in m/s.',
                        default=-200., type=float, required=False)
    parser.add_argument('--north_wind', help=f'Velocity of wind to the north at 100km in m/s.',
                        default=0., type=float, required=False)
    parser.add_argument('--ncpu', help='Number of CPUs.',
                        default=1, type=int, required=True)
    parser.add_argument('--time_block_size', help='Number of time steps to simulate at once (must be >= 2).',
                        default=2, type=int, required=True)


if __name__ == '__main__':
    if len(sys.argv) == 1:
        debug_main()
        exit(0)
    parser = argparse.ArgumentParser(
        description='Infers the value of DTEC and a constant over a screen.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    add_args(parser)
    flags, unparsed = parser.parse_known_args()
    logger.info("Running with:")
    for option, value in vars(flags).items():
        logger.info("\t{} -> {}".format(option, value))
    main(**vars(flags))