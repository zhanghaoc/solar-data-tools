""" Capacity Change Algorithm Module

This module the algorithm for detecting capacity changes in an unlabeled PV
power production data sets. The algorithm works as follows:

    - Run daily quantile statistic on cleaned data
    - Fit a signal demixing model, assuming a seasonal component and a piecewise
      constant component
    - Polish the L1 heuristic used to estimate piecewise constant component
      using iterative reweighting
    - Assign daily cluster labels using DBSCAN algorithm



"""

import numpy as np
from solardatatools.signal_decompositions import l1_l1d1_l2d2p365
from sklearn.cluster import DBSCAN


class CapacityChange:
    def __init__(self):
        self.metric = None
        self.s1 = None
        self.s2 = None
        self.labels = None

    def run(
        self,
        data,
        filter=None,
        quantile=1.00,
        c1=40e-6,  # scaled weights for QSS
        c2=6561e-6,
        c3=1e-6,
        reweight_eps=0.5,
        reweight_niter=5,
        dbscan_eps=0.02,
        dbscan_min_samples="auto",
        solver=None
    ):
        if filter is None:
            filter = np.ones(data.shape[1], dtype=bool)
        if np.sum(filter) > 0:
            metric = np.nanquantile(data, q=quantile, axis=0)
            metric /= np.max(metric)

            s1, s2, s3 = l1_l1d1_l2d2p365(
                metric,
                use_ixs=filter,
                w1=c1,
                w2=c2,
                w3=c3,
                solver=solver,
                sum_card=False
            )
        else:
            # print('No valid values! Please check your data and filter.')
            return

        # Get capacity assignments
        rounded_s1 = np.round(s1, 1)
        set_labels = list(set(rounded_s1))
        capacity_assignments = [set_labels.index(i) for i in rounded_s1]

        self.metric = metric
        self.s1 = s1
        self.s2 = s2
        self.s3 = s3
        self.labels = capacity_assignments