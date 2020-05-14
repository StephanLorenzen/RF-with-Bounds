import numpy as np
from sklearn.utils import check_random_state

from . import util
from .bounds import SH, PBkl, optimizeLamb, C1, C2, C3, MV2, optimizeMV2, MV2u, optimizeMV2u
from math import ceil

class MVBounds:
    def __init__(
            self,
            estimators,
            rho=None,
            sample_mode=None, # | 'bootstrap' | 'dim' | int | float
            random_state=None,
            ):
        self._estimators = estimators
        m                = len(estimators)  
        self._sample_mode= sample_mode
        self._prng       = check_random_state(random_state)
        self._rho = rho
        if rho is None:
            self._rho = util.uniform_distribution(m)
        assert(self._rho.shape[0]==m)

        # Some fitting stats
        self._OOB = None
        self._classes = None

    def fit(self, X, Y):
        X, Y = np.array(X), np.array(Y)
        self._classes = np.unique(Y)

        # No sampling
        if self._sample_mode is None:
            self._OOB = None

            for est in self._estimators:
                est.fit(X, Y)
            return None
        
        else:
            self._OOB = { }
            

            preds = []
            n = X.shape[0]
            m = len(self._estimators)
            
            n_sample = None
            if self._sample_mode=='bootstrap':
                n_sample = n
            elif self._sample_mode=='dim':
                n_sample = X.shape[1]+1
            elif type(self._sample_mode) is int:
                n_sample = self._sample_mode
            elif type(self._sample_mode) is float:
                n_sample = ceil(X.shape[0]*self._sample_mode)
            else:
                Utils.warn('Warning, fit: unknown sample_type')
                return None

            for est in self._estimators:
                oob_idx, oob_X = None, None
                
                # Sample points for training (w. replacement)
                while True: 
                    # Repeat until at least one example of each class
                    # (mostly relevant if n_sample is very small)
                    t_idx = self._prng.randint(n, size=n)\
                            if self._sample_mode=='bootstrap'\
                            else self._prng.choice(n, n_sample, replace=False)
                    t_X   = X[t_idx]
                    t_Y   = Y[t_idx]
                    if np.unique(t_Y).shape[0] > 1:
                        break

                # OOB sample
                oob_idx = np.delete(np.arange(n),np.unique(t_idx))
                oob_X   = X[oob_idx]

                # Fit this estimator
                est.fit(t_X, t_Y)
                # Predict on OOB
                oob_P = est.predict(oob_X)

                # Save predictions on oob and validation set for later
                preds.append((oob_idx, oob_P)) 
            
            risks,n,disagreements,tandem_risks,n2 = util.oob_stats(preds, Y)

            self._OOB['n']             = n
            self._OOB['n2']            = n2
            self._OOB['risks']         = risks
            self._OOB['disagreements'] = disagreements
            self._OOB['tandem_risks']  = tandem_risks
            
            return util.oob_estimate(self._rho, preds, Y)

    def predict(self, X, Y=None):
        n = X.shape[0]

        P = self.predict_all(X)
        mvP = util.mv_preds(self._rho, P)
        return (mvP, util.risk(mvP, Y)) if Y is not None else mvP
        

    def predict_all(self, X):
        n = X.shape[0]
        m = len(self._estimators)
        
        P = []
        for est in self._estimators:
            P.append(est.predict(X))
        
        return np.array(P)

    def optimize_rho(self, bound, val_data=None, unlabeled_data=None, incl_oob=True):
        if bound not in {"Lambda", "MV2"}:
            util.warn('Warning, optimize_rho: unknown bound!')
            return None
        if val_data is None and not incl_oob:
            util.warn('Warning, stats: Missing data!')
            return None

        stats = self.stats(val_data, unlabeled_data, incl_oob)
        if(bound=='Lambda'):
            (optLambda, rho, lam) = optimizeLamb(stats['risks'], stats['n_min'])
            self._rho = rho
            return (optLambda, rho, lam)
        elif(bound=='MV2'):
            if unlabeled_data is None:
                (optMV2, rho, lam) = optimizeMV2(stats['tandem_risks'], stats['n2_min'])
                self._rho = rho
                return (optMV2, rho, lam)
            else:
                (optMV2, rho, lam, gam) = optimizeMV2u(stats['risks'], stats['u_disagreements'], stats['n_min'], stats['u_n2_min'])
                self._rho = rho
                return (optMV2, rho, lam, gam)

    def bounds(self, val_data=None, unlabeled_data=None, incl_oob=True, stats=None):
        if stats is None:
            incl_oob = incl_oob and self._bootstrap
            if val_data is None and not incl_oob:
                util.warn('Warning, stats: Missing data!')
                return None
        
            stats = self.stats(val_data, unlabeled_data, incl_oob)
        
        C = self._classes.shape[0]

        pi = util.uniform_distribution(len(self._estimators))
        KL = util.kl(self._rho, pi)
        bounds = dict()
        bounds["PBkl"] = PBkl(stats['risk'], stats['n_min'], KL)
        if C==2:
            bounds["C1"] = C1(stats['risk'], stats['disagreement'], stats['n_min'], stats['n2_min'], KL)
            bounds["C2"] = C2(stats['tandem_risk'], stats['disagreement'], stats['n2_min'], KL)
        bounds["MV2"] = MV2(stats['tandem_risk'], stats['n2_min'], KL)
        
        if val_data is not None:
            bounds['SH'] = SH(stats['mv_risk'], stats['n_val'])
        
        if unlabeled_data is not None and C==2:
            bounds['MV2u'] = MV2u(stats['risk'], stats['u_disagreement'], stats['n_min'], stats['u_n2_min'], KL)
            
        return bounds

    def stats(self, val_data=None, unlabeled_data=None, incl_oob=True):
        incl_oob = incl_oob and self._sample_mode is not None
        if val_data is None and not incl_oob:
            util.warn('Warning, RandomForestWithBound.stats: Missing data!')
            return None

        m             = len(self._estimators)
        n, n2         = np.zeros((m,)), np.zeros((m,m))
        risks         = np.zeros((m,))
        disagreements = np.zeros((m,m))
        tandem_risks  = np.zeros((m,m))
        
        if incl_oob:
            n             += self._OOB['n']
            n2            += self._OOB['n2']
            risks         += self._OOB['risks']
            disagreements += self._OOB['disagreements']
            tandem_risks  += self._OOB['tandem_risks']

        mv_risk = None
        if val_data is not None:
            assert(len(val_data)==2)
            valX,valY = val_data

            valP = self.predict_all(valX)

            n             += valX.shape[0]
            n2            += valX.shape[0]
            risks         += util.gibbs(valP, valY)
            disagreements += util.disagreements(valP)
            tandem_risks  += util.tandem_risks(valP, valY)
        
            mv_risk = util.mv_risk(self._rho, valP, valY)

        stats = {
                'risk':np.average(risks/n, weights=self._rho),
                'risks':risks/n,
                'n':n,
                'n_min':np.min(n),
                'disagreement':np.average(np.average(disagreements/n2, weights=self._rho, axis=1), weights=self._rho),
                'disagreements':disagreements/n2,
                'tandem_risk':np.average(np.average(tandem_risks/n2, weights=self._rho, axis=1), weights=self._rho),
                'tandem_risks':tandem_risks/n2,
                'n2':n2,
                'n2_min':np.min(n2)
                }

        if val_data is not None:
            stats['mv_risk'] = mv_risk
            stats['n_val']   = valX.shape[0]

        if unlabeled_data is not None:
            ulP = self.predict_all(unlabeled_data)
            u_disagreements  = np.copy(disagreements) + util.disagreements(ulP)
            u_n2             = np.copy(n2) + ulP.shape[1]

            stats['u_disagreement'] = np.average(np.average(u_disagreements/u_n2, weights=self._rho, axis=1), weights=self._rho)
            stats['u_disagreements'] = u_disagreements/u_n2
            stats['u_n2'] = u_n2
            stats['u_n2_min'] = np.min(u_n2)

        return stats