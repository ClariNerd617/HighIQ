import numpy as np
import cupy as cp
import xarray as xr


def _gpu_calc_power(psd, dV, block_size=200, normed=True):
    shp = psd.shape
    times = shp[0]
    power = np.zeros((shp[0], shp[1]))
    if len(shp) == 3:
        for k in range(0, times, block_size):
            the_max = min([k + block_size, times])
            gpu_array = cp.array(psd[k:the_max, :, :])
            if normed:
                gpu_array = 10 ** (gpu_array / 10. * dV)
            gpu_array = cp.sum(gpu_array, axis=2)
            power[k:the_max] = gpu_array.get()
    else:
        gpu_array = cp.array(psd)
        if normed:
            gpu_array = 10 ** (gpu_array / 10. * dV)
        gpu_array = cp.sum(gpu_array, axis=1)
        power = gpu_array.get()

    return power


def _gpu_calc_velocity(psd, power, vel_bins, dV, block_size=100):
    shp = psd.shape
    times = shp[0]
    velocity = np.zeros((shp[0], shp[1]))

    for k in range(0, times, block_size):
        the_max = min([k + block_size, times])
        gpu_array = cp.array(psd[k:the_max, :, :], dtype=float)
        power_array = cp.array(power[k:the_max, :], dtype=float)
        vel_bins_tiled = cp.tile(vel_bins, (the_max - k, shp[1], 1))
        gpu_array = 10 ** (gpu_array / 10. * dV)
        gpu_array = 1 / power_array * cp.sum(gpu_array * vel_bins_tiled, axis=2)
        velocity[k:the_max, :] = gpu_array.get()
    return velocity


def _gpu_calc_velocity_dumb(psd, vel_bins, block_size=500):
    shp = psd.shape
    times = shp[0]
    velocity = np.zeros((shp[0], shp[1]))
    dV = np.diff(vel_bins)[0]
    vel_min = vel_bins.min()
    for k in range(0, times, block_size):
        the_max = min([k + block_size, times])
        gpu_array = cp.array(psd[k:the_max, :, :])
        gpu_array = cp.argmax(gpu_array, axis=2)
        gpu_array = vel_min + gpu_array * dV
        velocity[k:the_max, :] = gpu_array.get()
    return velocity


def _gpu_calc_spectral_width(psd, power, vel_bins, velocity, dV, block_size=100):
    shp = psd.shape
    times = shp[0]
    specwidth = np.zeros((shp[0], shp[1]))
    for k in range(0, times, block_size):
        the_max = min([k+block_size, times])
        gpu_array = cp.array(psd[k:the_max, :, :], dtype=float)
        power_array = cp.array(power[k:the_max, :], dtype=float)
        velocity_array = cp.array(velocity[k:the_max, :])
        velocity_array = cp.transpose(cp.tile(velocity_array, (shp[2], 1, 1)), [1, 2, 0])
        vel_bins_tiled = cp.tile(vel_bins, (the_max-k, shp[1], 1))
        gpu_array = 10**(gpu_array/10.*dV)
        gpu_array = cp.sqrt(1 / power_array *
                            cp.sum((vel_bins_tiled - velocity_array)**2 * gpu_array, axis=2))
        specwidth[k:the_max, :] = gpu_array.get()
    return specwidth


def _gpu_snr(power, noise, block_size=200):
    shp = power.shape
    power_array = cp.array(power)
    gpu_noise = cp.array(noise)
    power_array = 10*cp.log10(power_array/gpu_noise)
    snr = power_array.get()
    return snr


def _gpu_calc_skewness(psd, power, vel_bins, velocity, spec_width, dV, block_size=100):
    shp = psd.shape
    times = shp[0]
    skewness = np.zeros((shp[0], shp[1]))
    for k in range(0, times, block_size):
        the_max = min([k+block_size, times])
        gpu_array = cp.array(psd[k:the_max, :, :], dtype=float)
        power_array = cp.array(power[k:the_max, :], dtype=float)
        spec_width_array = cp.array(spec_width[k:the_max, :], dtype=float)
        power_array *= spec_width_array**3
        del spec_width_array
        velocity_array = cp.array(velocity[k:the_max, :])
        velocity_array = cp.transpose(cp.tile(velocity_array, (shp[2], 1, 1)), [1, 2, 0])
        vel_bins_tiled = cp.tile(vel_bins, (the_max-k, shp[1], 1))
        gpu_array = 10**(gpu_array/10.*dV)
        gpu_array = 1/power_array*cp.sum((vel_bins_tiled - velocity_array)**3 * gpu_array, axis=2)
        skewness[k:the_max, :] = gpu_array.get()
    return skewness


def _gpu_calc_kurtosis(psd, power, vel_bins, velocity, spec_width, dV, block_size=100):
    shp = psd.shape
    times = shp[0]
    kurtosis = np.zeros((shp[0], shp[1]))
    for k in range(0, times, block_size):
        the_max = min([k+block_size, times])
        gpu_array = cp.array(psd[k:the_max, :, :], dtype=float)
        power_array = cp.array(power[k:the_max, :], dtype=float)
        spec_width_array = cp.array(spec_width[k:the_max, :], dtype=float)
        power_array *= spec_width_array**4
        velocity_array = cp.array(velocity[k:the_max, :])
        velocity_array = cp.transpose(cp.tile(velocity_array, (shp[2], 1, 1)), [1, 2, 0])
        vel_bins_tiled = cp.tile(vel_bins, (the_max-k, shp[1], 1))
        gpu_array = 10**(gpu_array/10.*dV)
        gpu_array = 1/power_array*cp.sum((vel_bins_tiled - velocity_array)**4 * gpu_array, axis=2)
        kurtosis[k:the_max, :] = gpu_array.get()
    return kurtosis


def get_lidar_moments(spectra, snr_thresh=0, block_size_ratio=1.0, which_moments=None):
    """
    This function will retrieve the lidar moments of the Doppler spectra.

    Parameters
    ----------
    spectra: ACT Dataset
        The dataset containing the processed Doppler spectral density functions.
    snr_thresh: float
        The minimum signal to noise ratio to use as an initial mask of noise.
    block_size_ratio: float
        This value is used to determine how much data the GPU will process in one loop. If your
        GPU has more memory, you may be able to optimize processing by raising this number. In
        addition, if you encounter out of memory errors, try lowering this number, ensuring that
        it is a positive floating point number.
    which_moments: list or None
        This tells HighIQ which moments should be processed. If this list is None, then the
        signal to noise ratio, doppler velocity, spectral width, skewness,
        and kurtosis will be calculated.

    Returns
    -------
    spectra: ACT Dataset
        The database with the Doppler lidar moments.
    """
    if which_moments is None:
        which_moments = ['snr', 'doppler_velocity', 'spectral_width',
                         'skewness', 'kurtosis']
    else:
        which_moments = [x.lower() for x in which_moments]

    if not block_size_ratio > 0:
        raise ValueError("block_size_ratio must be a positive floating point number!")

    dV = np.diff(spectra['vel_bins'])[0]
    linear_psd = spectra['power_spectral_density_interp']
    linear_psd_0filled = linear_psd.fillna(0)
    power = _gpu_calc_power(
        linear_psd_0filled, dV, block_size=round(200 * block_size_ratio))
    if ('doppler_velocity' in which_moments or 'spectral_wifth' in which_moments or
        'skewness' in which_moments or 'kurtosis' in which_moments):
        velocity = _gpu_calc_velocity(
            linear_psd_0filled, power,
            spectra['vel_bin_interp'].values, dV, block_size=round(100*block_size_ratio))

    spectra['noise'] = spectra['power_bkg'].sum(axis=2)
    if 'snr' in which_moments:
        power_with_noise = _gpu_calc_power(
            spectra['power'].values, dV,
            normed=False, block_size=round(200 * block_size_ratio))
        power_with_noise = xr.DataArray(power_with_noise, dims=('time', 'range'))
        spectra['snr'] = xr.DataArray(
            _gpu_snr(power_with_noise, spectra['noise'].values),
            dims=('time', 'range'))
        spectra['snr'].attrs['long_name'] = "Signal to Noise Ratio"
        spectra['snr'].attrs['units'] = "dB"
        spectra['intensity'] = spectra['snr'] + 1.
        spectra['intensity'].attrs['long_name'] = "Intensity (SNR + 1)"
        spectra['intensity'].attrs['units'] = "dB"
        spectra['intensity'] = \
            spectra['intensity'].where(spectra.snr > snr_thresh)
        spectra.attrs['snr_mask'] = "%f dB" % snr_thresh

    if 'doppler_velocity' in which_moments:
        velocity_dumb = _gpu_calc_velocity_dumb(
            linear_psd_0filled, spectra['vel_bin_interp'].values,
            block_size=round(500*block_size_ratio))
        spectra['doppler_velocity_max_peak'] = xr.DataArray(
            velocity_dumb, dims=('time', 'range'))
        spectra['doppler_velocity_max_peak'].attrs['long_name'] = \
            "Doppler velocity derived using location of highest " \
            "peak in spectra."
        spectra['doppler_velocity_max_peak'].attrs["units"] = "m s-1"
        spectra['doppler_velocity'] = xr.DataArray(
            velocity, dims=('time', 'range'))
        spectra['doppler_velocity'].attrs['long_name'] = \
            "Doppler velocity using first moment"
        spectra['doppler_velocity'].attrs['units'] = "m s-1"
        spectra['doppler_velocity_max_peak'] = \
            spectra['doppler_velocity_max_peak'].where(spectra.snr > snr_thresh)
        spectra['doppler_velocity'] = spectra['doppler_velocity'].where(
            spectra.snr > snr_thresh)

    if 'spectral_width' in which_moments or 'kurtosis' in which_moments or 'skewness' in which_moments:
        spectral_width = _gpu_calc_spectral_width(
            linear_psd, power, spectra['vel_bin_interp'].values,
            velocity, dV, block_size=round(100 * block_size_ratio))

    if 'spectral_width' in which_moments:
        spectra['spectral_width'] = xr.DataArray(
            spectral_width, dims=('time', 'range'))
        spectra['spectral_width'].attrs["long_name"] = "Spectral width"
        spectra['spectral_width'].attrs["units"] = "m s-1"
        if 'snr' in which_moments:
            spectra['spectral_width'] = spectra['spectral_width'].where(spectra.snr > snr_thresh)

    if 'skewness' in which_moments:
        skewness = _gpu_calc_skewness(
            linear_psd, power, spectra['vel_bin_interp'].values, velocity, spectral_width, dV,
            block_size=round(100 * block_size_ratio))
        spectra['skewness'] = xr.DataArray(skewness, dims=('time', 'range'))
        if 'snr' in which_moments:
            spectra['skewness'] = spectra['skewness'].where(spectra.snr > snr_thresh)
        spectra['skewness'].attrs["long_name"] = "Skewness"
        spectra['skewness'].attrs["units"] = "m^3 s^-3"

    if 'kurtosis' in which_moments:
        kurtosis = _gpu_calc_kurtosis(
            linear_psd, power, spectra['vel_bin_interp'].values, velocity, spectral_width, dV,
            block_size=round(100 * block_size_ratio))
        spectra['kurtosis'] = xr.DataArray(kurtosis, dims=('time', 'range'))
        if 'snr' in which_moments:
            spectra['kurtosis'] = spectra['kurtosis'].where(spectra.snr > snr_thresh)
        spectra['skewness'].attrs["long_name"] = "Kurtosis"
        spectra['skewness'].attrs["units"] = "m^4 s^-4"

    spectra['range'].attrs['long_name'] = "Range"
    spectra['range'].attrs['units'] = 'm'
    spectra['vel_bins'].attrs['long_name'] = "Doppler velocity"
    spectra['vel_bins'].attrs['units'] = 'm s-1'
    return spectra