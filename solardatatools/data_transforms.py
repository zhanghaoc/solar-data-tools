''' Data Transforms Module

This module contains functions for transforming PV power data, including time-axis standardization and
2D-array generation

'''

from datetime import timedelta
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from sklearn.neighbors.kde import KernelDensity

from solardatatools.clear_day_detection import find_clear_days
from solardatatools.utilities import total_variation_filter, total_variation_plus_seasonal_filter

def standardize_time_axis(df, datetimekey='Date-Time'):
    '''
    This function takes in a pandas data frame containing tabular time series data, likely generated with a call to
    pandas.read_csv(). It is assumed that each row of the data frame corresponds to a unique date-time, though not
    necessarily on standard intervals. This function will attempt to convert a user-specified column containing time
    stamps to python datetime objects, assign this column to the index of the data frame, and then standardize the
    index over time. By standardize, we mean reconstruct the index to be at regular intervals, starting at midnight of
    the first day of the data set. This solves a couple common data errors when working with raw data. (1) Missing data
    points from skipped scans in the data acquisition system. (2) Time stamps that are at irregular exact times,
    including fractional seconds.

    :param df: A pandas data frame containing the tabular time series data
    :param datetimekey: An optional key corresponding to the name of the column that contains the time stamps
    :return: A new data frame with a standardized time axis
    '''
    # convert index to timeseries
    try:
        df['Date-Time'] = pd.to_datetime(df['Date-Time'])
        df.set_index('Date-Time', inplace=True)
    except KeyError:
        time_cols = [col for col in df.columns if np.logical_or('Time' in col, 'time' in col)]
        key = time_cols[0]
        df[key] = pd.to_datetime(df[key])
        df.set_index(key, inplace=True)
    # standardize the timeseries axis to a regular frequency over a full set of days
    diff = (df.index[1:] - df.index[:-1]).seconds
    freq = int(np.median(diff))  # the number of seconds between each measurement
    start = df.index[0]
    end = df.index[-1]
    time_index = pd.date_range(start=start.date(), end=end.date() + timedelta(days=1), freq='{}s'.format(freq))[:-1]
    df = df.reindex(index=time_index, method='nearest')
    return df.fillna(value=0)

def make_2d(df, key='dc_power'):
    '''
    This function constructs a 2D array (or matrix) from a time series signal with a standardized time axis. The data is
    chunked into days, and each consecutive day becomes a column of the matrix.

    :param df: A pandas data frame contained tabular data with a standardized time axis.
    :param key: The key corresponding to the column in the data frame contained the signal to make into a matrix
    :return: A 2D numpy array with shape (measurements per day, days in data set)
    '''
    if df is not None:
        n_steps = int(24 * 60 * 60 / df.index.freq.n)
        D = df[key].values.reshape(n_steps, -1, order='F')
        return D
    else:
        return

def fix_time_shifts(data, verbose=False, return_ixs=False):
    D = data
    #################################################################################################################
    # Part 1: Detecting the days on which shifts occurs. If no shifts are detected, the algorithm exits, returning
    # the original data array. Otherwise, the algorithm proceeds to Part 2.
    #################################################################################################################
    # Find "center of mass" of each day's energy content. This generates a 1D signal from the 2D input signal.
    div1 = np.dot(np.linspace(0, 24, D.shape[0]), D)
    div2 = np.sum(D, axis=0)
    s1 = np.empty_like(div1)
    s1[:] = np.nan
    msk = div2 != 0
    s1[msk] = np.divide(div1[msk], div2[msk])
    # Apply a clear day filter
    m = find_clear_days(D)
    s1_f = np.empty_like(s1)
    s1_f[:] = np.nan
    s1_f[m] = s1[m]
    # Apply total variation filter with seasonal baseline
    s2, s_seas = total_variation_plus_seasonal_filter(s1_f, c1=10, c2=500)
    # Perform clustering with KDE
    kde = KernelDensity(kernel='gaussian', bandwidth=0.05).fit(s2[:, np.newaxis])
    X_plot = np.linspace(0.95 * np.min(s2), 1.05 * np.max(s2))[:, np.newaxis]
    log_dens = kde.score_samples(X_plot)
    mins = argrelextrema(log_dens, np.less)[0]
    maxs = argrelextrema(log_dens, np.greater)[0]
    # Drop clusters with too few members
    keep = np.ones_like(maxs, dtype=np.bool)
    for ix, mx in enumerate(maxs):
        if np.exp(log_dens)[mx] < 1e-1:
            keep[ix] = 0
    mx_keep = maxs[keep]
    mx_drop = maxs[~keep]
    mn_drop = []
    for md in mx_drop:
        dists = np.abs(X_plot[:, 0][md] - X_plot[:, 0][mx_keep])
        max_merge = mx_keep[np.argmin(dists)]
        for mn in mins:
            cond1 = np.logical_and(mn < max_merge, mn > md)
            cond2 = np.logical_and(mn > max_merge, mn < md)
            if np.logical_or(cond1, cond2):
                print('merge', md, 'with', max_merge, 'by dropping', mn)
                mn_drop.append(mn)
    mins_new = np.array([i for i in mins if i not in mn_drop])
    # Assign cluster labels to days in data set
    clusters = np.zeros_like(s1)
    if len(mins_new) > 0:
        for it, ex in enumerate(X_plot[:, 0][mins_new]):
            m = s2 >= ex
            clusters[m] = it + 1
    # Identify indices corresponding to days when time shifts occurred
    index_set = np.arange(D.shape[1]-1)[clusters[1:] != clusters[:-1]] + 1
    if len(index_set) == 0:
        if verbose:
            print('No time shifts found')
        if return_ixs:
            return D, []
        else:
            return D
    #################################################################################################################
    # Part 2: Fixing the time shifts.
    #################################################################################################################
    if verbose:
        print('Time shifts found at: ', index_set)
    ixs = np.r_[[None], index_set, [None]]
    # Take averages of solar noon estimates over the segments of the data set defined by the shift points
    A = []
    for i in range(len(ixs) - 1):
        avg = np.average(np.ma.masked_invalid(s1_f[ixs[i]:ixs[i + 1]]))
        A.append(np.round(avg * D.shape[0] / 24))
    A = np.array(A)
    # Considering the first segment as the reference point, determine how much to shift the remaining segments
    rolls = A[0] - A[1:]
    # Apply the corresponding shift to each segment
    Dout = np.copy(D)
    for ind, roll in enumerate(rolls):
        D_rolled = np.roll(D, int(roll), axis=0)
        Dout[:, ixs[ind + 1]:] = D_rolled[:, ixs[ind + 1]:]
    if return_ixs:
        return Dout, index_set
    else:
        return Dout
