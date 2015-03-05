#-*- coding: utf-8 -*-

from __future__ import print_function
from __future__ import division
import sys, os.path
pkg_dir = os.path.dirname(os.path.realpath(__file__)) + '/../../'
sys.path.append(pkg_dir)

import pyopencl.array
from scipy.stats import t
from collections import Counter
from MPBNP import *

np.set_printoptions(suppress=True)

class CollapsedGibbs(BaseSampler):

    def __init__(self, cl_mode = True, alpha = 1.0, cl_device = None, record_best = True):
        """Initialize the class.
        """
        BaseSampler.__init__(self, record_best, cl_mode, cl_device)

        if cl_mode:
            program_str = open(pkg_dir + 'MPBNP/crp/kernels/crp_cl.c', 'r').read()
            self.prg = cl.Program(self.ctx, program_str).build()

        # set some prior hyperparameters
        self.alpha = alpha
        self.gamma_alpha0 = 1.
        self.gamma_beta0 = 1.
        self.gaussian_mu0 = 1.
        self.gaussian_k0 = 0.001

    def read_csv(self, filepath, header=True):
        """Read the data from a csv file.
        """
        BaseSampler.read_csv(self, filepath, header)
        # convert the data to floats
        self.new_obs = []
        for row in self.obs:
            self.new_obs.append([float(_) for _ in row])
        self.obs = np.array(self.new_obs).astype(np.float32)
        return
        
    def do_inference(self, init_labels = None, output_file = None):
        """Perform inference on the given observations assuming 
        data are generated by a Gaussian CRP Mixture Model.
        """
        BaseSampler.do_inference(self, output_file)
        if init_labels is None:
            init_labels = np.random.randint(low = 0, high = min(self.obs.shape[0], 10), 
                                            size = self.obs.shape[0]).astype(np.int32)
        else:
            init_labels = init_labels.astype(np.int32)

        if self.cl_mode:
            return self.cl_infer_kdgaussian(init_labels = init_labels, output_file = output_file)
        else:
            return self.infer_kdgaussian(init_labels = init_labels, output_file = output_file)

    def infer_1dgaussian(self, init_labels, output_file = None):
        """Perform inference on class labels assuming data are 1-d gaussian,
        without OpenCL acceleration.
        """
        total_time = 0
        a_time = time()

        cluster_labels = init_labels
        if self.record_best:
            self.auto_save_sample(cluster_labels)
        
        if output_file is not None: print(*xrange(self.N), file = output_file, sep = ',')
        for i in xrange(self.niter):

            if output_file is not None and i >= self.burnin and not self.record_best: 
                print(*cluster_labels, file = output_file, sep = ',')  
            # identify existing clusters and generate a new one
            uniq_labels = np.unique(cluster_labels)
            _, _, new_cluster_label = smallest_unused_label(uniq_labels)
            uniq_labels = np.hstack((new_cluster_label, uniq_labels))

            # compute the sufficient statistics of each cluster
            logpost = np.empty((self.N, uniq_labels.shape[0]))
            
            for label_index in xrange(uniq_labels.shape[0]):
                label = uniq_labels[label_index]
                if label == new_cluster_label:
                    n, mu, var = 0, 0, 0
                else:
                    cluster_obs = self.obs[np.where(cluster_labels == label)]
                    n = cluster_obs.shape[0]
                    mu = np.mean(cluster_obs)
                    var = np.var(cluster_obs)

                k_n = self.gaussian_k0 + n
                mu_n  = (self.gaussian_k0 * self.gaussian_mu0 + n * mu) / k_n
                alpha_n = self.gamma_alpha0 + n / 2
                beta_n = self.gamma_beta0 + 0.5 * var * n + \
                    self.gaussian_k0 * n * (mu - self.gaussian_mu0) ** 2 / (2 * k_n)
                Lambda = alpha_n * k_n / (beta_n * (k_n + 1))
            
                t_frozen = t(df = 2 * alpha_n, loc = mu_n, scale = (1 / Lambda) ** 0.5)
                logpost[:,label_index] = t_frozen.logpdf(self.obs[:,0])
                logpost[:,label_index] += np.log(n/(self.N + self.alpha)) if n > 0 else np.log(self.alpha/(self.N+self.alpha))
            
            # sample and implement the changes
            temp_cluster_labels = np.empty(cluster_labels.shape, dtype=np.int32)
            for n in xrange(self.N):
                target_cluster = sample(a = uniq_labels, p = lognormalize(logpost[n]))
                temp_cluster_labels[n] = target_cluster

            if self.record_best:
                if self.auto_save_sample(temp_cluster_labels):
                    cluster_labels = temp_cluster_labels
                if self.no_improvement():
                    break                    
            else:
                cluster_labels = temp_cluster_labels
                
        if output_file is not None and self.record_best: 
            print(*self.best_sample[0], file = output_file, sep = ',')

        self.total_time += time() - a_time
        return self.gpu_time, self.total_time, Counter(cluster_labels).most_common()

    def cl_infer_1dgaussian(self, init_labels, output_file = None):
        """Implementing concurrent sampling of class labels with OpenCL.
        """
        d_hyper_param = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, 
                                  hostbuf = np.array([self.gaussian_mu0, self.gaussian_k0, 
                                                      self.gamma_alpha0, self.gamma_beta0, self.alpha]).astype(np.float32))

        cluster_labels = init_labels
        if self.record_best:
            self.auto_save_sample(cluster_labels)

        d_data = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = self.obs[:,0])
        d_labels = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = cluster_labels)
        
        if output_file is not None: print(*xrange(self.N), file = output_file, sep = ',')
        total_a_time = time()

        for i in xrange(self.niter):
            if output_file is not None and i >= self.burnin and not self.record_best: 
                print(*cluster_labels, file = output_file, sep = ',')            
        
            uniq_labels = np.unique(cluster_labels)
            _, _, new_cluster_label = smallest_unused_label(uniq_labels)
            uniq_labels = np.hstack((new_cluster_label, uniq_labels)).astype(np.int32)
            suf_stats = np.empty((uniq_labels.shape[0], 4))
            
            for label_index in xrange(uniq_labels.shape[0]):
                label = uniq_labels[label_index]
                if label == new_cluster_label:
                    suf_stats[label_index] = (label, 0, 0, 0)
                else:
                    cluster_obs = self.obs[np.where(cluster_labels == label)]
                    cluster_mu = np.mean(cluster_obs)
                    cluster_ss = np.var(cluster_obs) * cluster_obs.shape[0]
                    suf_stats[label_index] = (label, cluster_mu, cluster_ss, cluster_obs.shape[0])

            gpu_a_time = time()
            d_uniq_label = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = uniq_labels)
            d_mu = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, 
                             hostbuf = suf_stats[:,1].astype(np.float32))
            d_ss = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, 
                             hostbuf = suf_stats[:,2].astype(np.float32))
            d_n = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, 
                            hostbuf = suf_stats[:,3].astype(np.int32))
            d_logpost = cl.array.empty(self.queue,(self.obs.shape[0], uniq_labels.shape[0]), np.float32)
            d_rand = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, 
                               hostbuf = np.random.random(self.obs.shape).astype(np.float32))

            if self.device_type == cl.device_type.CPU:
                self.prg.normal_1d_logpost_loopy(self.queue, self.obs.shape, None,
                                                 d_labels, d_data, d_uniq_label, d_mu, d_ss, d_n, 
                                                 np.int32(uniq_labels.shape[0]), d_hyper_param, d_rand,
                                                 d_logpost.data)
            else:
                self.prg.normal_1d_logpost(self.queue, (self.obs.shape[0], uniq_labels.shape[0]), None,
                                           d_labels, d_data, d_uniq_label, d_mu, d_ss, d_n, 
                                           np.int32(uniq_labels.shape[0]), d_hyper_param, d_rand,
                                           d_logpost.data)
                self.prg.resample_labels(self.queue, (self.obs.shape[0],), None,
                                         d_labels, d_uniq_label, np.int32(uniq_labels.shape[0]),
                                         d_rand, d_logpost.data)

            temp_cluster_labels = np.empty(cluster_labels.shape, dtype=np.int32)
            cl.enqueue_copy(self.queue, temp_cluster_labels, d_labels)
            self.gpu_time += time() - gpu_a_time

            if self.record_best:
                if self.auto_save_sample(temp_cluster_labels):
                    cluster_labels = temp_cluster_labels
                if self.no_improvement():
                    break                    

            else:
                cluster_labels = temp_cluster_labels

        if output_file is not None and self.record_best: 
            print(*self.best_sample[0], file = output_file, sep = ',')
            
        self.total_time += time() - total_a_time
        return self.gpu_time, self.total_time, Counter(cluster_labels).most_common()

    def infer_kdgaussian(self, init_labels, output_file = None):
        """Implementing concurrent sampling of partition labels without OpenCL.
        """
        try: dim = self.obs.shape[1]
        except IndexError: 
            dim = 1
        if dim == 1:
            return self.infer_1dgaussian(init_labels = init_labels, output_file = output_file)

        data_size = self.obs.shape[0]

        # set some prior hyperparameters
        wishart_v0 = dim
        wishart_T0 = np.identity(dim)
        gaussian_k0 = 0.01
        gaussian_mu0 = np.zeros(dim)

        cluster_labels = init_labels.astype(np.int32)

        if output_file is not None: print(*xrange(data_size), file = output_file, sep = ',')
        total_time = 0
        for i in xrange(self.niter):
            a_time = time()
            if output_file is not None and i >= burnin: 
                print(*cluster_labels, file = output_file, sep = ',')            
            # at the beginning of each iteration, identify the unique cluster labels
            uniq_labels = np.unique(cluster_labels)
            _, _, new_cluster_label = smallest_unused_label(uniq_labels)
            uniq_labels = np.hstack((new_cluster_label, uniq_labels))
            #num_of_clusters = uniq_labels.shape[0]

            # compute the sufficient statistics of each cluster
            n = np.empty(uniq_labels.shape)
            mu = np.empty((uniq_labels.shape[0], dim))
            cov_mu0 = np.empty((uniq_labels.shape[0], dim, dim))
            cov_obs = np.empty((uniq_labels.shape[0], dim, dim))
            logpost = np.empty((data_size, uniq_labels.shape[0]))

            for label_index in xrange(uniq_labels.shape[0]):
                label = uniq_labels[label_index]
                if label == new_cluster_label:
                    cov_obs[label_index], cov_mu0[label_index] = (0,0)
                    mu[label_index], n[label_index] = (0,0)
                else:
                    cluster_obs = self.obs[np.where(cluster_labels == label)]
                    mu[label_index] = np.mean(cluster_obs, axis = 0)
                    obs_deviance = cluster_obs - mu[label_index]
                    mu0_deviance = np.reshape(gaussian_mu0 - mu[label_index], (dim, 1))
                    cov_obs[label_index] = np.dot(obs_deviance.T, obs_deviance)
                    cov_mu0[label_index] = np.dot(mu0_deviance, mu0_deviance.T)
                    n[label_index] = cluster_obs.shape[0]
                kn = gaussian_k0 + n[label_index]
                vn = wishart_v0 + n[label_index]
                sigma = (wishart_T0 + cov_obs[label_index] + (gaussian_k0 * n[label_index]) / kn * cov_mu0[label_index]) * (kn + 1) / kn / (vn - dim + 1)
                det = np.linalg.det(sigma)
                inv = np.linalg.inv(sigma)
                df = vn - dim + 1

                logpost[:,label_index] = math.lgamma(df / 2.0 + dim / 2.0) - math.lgamma(df / 2.0) - 0.5 * np.log(det) - 0.5 * dim * np.log(df * math.pi) - \
                    0.5 * (df + dim) * np.log(1.0 + (1.0 / df) * np.dot(np.dot(self.obs - mu[label_index], inv), (self.obs - mu[label_index]).T).diagonal())
                logpost[:,label_index] += np.log(n[label_index]) if n[label_index] > 0 else np.log(self.alpha)
               
            # resample the labels and implement the changes
            for j in xrange(data_size):
                target_cluster = sample(a = uniq_labels, p = lognormalize(logpost[j]))
                cluster_labels[j] = target_cluster

            total_time += time() - a_time
            
        return -1.0, total_time, Counter(cluster_labels).most_common()

    def cl_infer_kdgaussian(self, init_labels, output_file = None):
        """Implementing concurrent sampling of class labels with OpenCL.
        """
        try: dim = np.int32(self.obs.shape[1])
        except IndexError: 
            dim = np.int32(1)
        if dim == 1:
            return self.cl_infer_1dgaussian(init_labels = init_labels, output_file = output_file)

        gpu_time, total_time = 0, 0
        data_size = np.int32(self.obs.shape[0])

        # set some prior hyperparameters
        wishart_v0 = np.float32(dim)
        wishart_T0 = np.identity(dim).astype(np.float32)
        gaussian_k0 = np.float32(0.01)
        gaussian_mu0 = np.zeros(dim).astype(np.float32)
        d_T0 = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = wishart_T0)

        cluster_labels = init_labels.astype(np.int32)

        # push data and initial labels onto the openCL device
        # data won't change, labels are modified on the device
        d_data = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = self.obs)
        d_labels = cl.Buffer(self.ctx, self.mf.READ_WRITE | self.mf.COPY_HOST_PTR, hostbuf = cluster_labels)

        if output_file is not None: print(*xrange(data_size), file = output_file, sep = ',')
        total_a_time = time()
        for i in xrange(self.niter):
            if output_file is not None and i >= self.burnin: 
                print(*cluster_labels, file = output_file, sep = ',')            
            # at the beginning of each iteration, identity the unique cluster labels
            uniq_labels = np.unique(cluster_labels)
            _, _, new_cluster_label = smallest_unused_label(uniq_labels)
            uniq_labels = np.hstack((new_cluster_label, uniq_labels)).astype(np.int32)
            num_of_clusters = np.int32(uniq_labels.shape[0])

            # compute the sufficient statistics of each cluster
            h_n = np.empty(uniq_labels.shape).astype(np.int32)
            h_mu = np.empty((uniq_labels.shape[0], dim)).astype(np.float32)
            h_cov_mu0 = np.empty((uniq_labels.shape[0], dim, dim)).astype(np.float32)
            h_cov_obs = np.empty((uniq_labels.shape[0], dim, dim)).astype(np.float32)
            h_sigma = np.empty((uniq_labels.shape[0], dim, dim)).astype(np.float32)

            for label_index in xrange(uniq_labels.shape[0]):
                label = uniq_labels[label_index]
                if label == new_cluster_label:
                    h_cov_obs[label_index], h_cov_mu0[label_index] = (0,0)
                    h_mu[label_index], h_n[label_index] = (0,0)
                else:
                    cluster_obs = self.obs[np.where(cluster_labels == label)]
                    h_mu[label_index] = np.mean(cluster_obs, axis = 0)
                    obs_deviance = cluster_obs - h_mu[label_index]
                    mu0_deviance = np.reshape(gaussian_mu0 - h_mu[label_index], (dim, 1))
                    h_cov_obs[label_index] = np.dot(obs_deviance.T, obs_deviance)
                    h_cov_mu0[label_index] = np.dot(mu0_deviance, mu0_deviance.T)
                    h_n[label_index] = cluster_obs.shape[0]
                #kn = gaussian_k0 + h_n[label_index]
                #vn = wishart_v0 + h_n[label_index]
                #h_sigma[label_index] = (wishart_T0 + h_cov_obs[label_index] + (gaussian_k0 * h_n[label_index]) / kn * h_cov_mu0[label_index]) * (kn + 1) / kn / (vn - dim + 1)
                    
            # using OpenCL to compute the log posterior of each item and perform resampling
            gpu_a_time = time()

            d_n = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = h_n)
            d_mu = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = h_mu)
            d_cov_mu0 = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = h_cov_mu0)
            d_cov_obs = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = h_cov_obs)
            d_sigma = cl.array.empty(self.queue, h_cov_obs.shape, np.float32)

            self.prg.normal_kd_sigma_matrix(self.queue, h_cov_obs.shape, None,
                                            d_n, d_cov_obs, d_cov_mu0, 
                                            d_T0, gaussian_k0, wishart_v0, d_sigma.data)
            
            # copy the sigma matrix to host memory and calculate determinants and inversions
            h_sigma = d_sigma.get()
            h_determinants = np.linalg.det(h_sigma).astype(np.float32)
            h_inverses = np.array([np.linalg.inv(_) for _ in h_sigma]).astype(np.float32)

            d_uniq_label = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = uniq_labels)
            d_determinants = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = h_determinants)
            d_inverses = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = h_inverses)
            d_rand = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = np.random.random(data_size).astype(np.float32))
            d_logpost = cl.array.empty(self.queue, (data_size, uniq_labels.shape[0]), np.float32)

            # if the OpenCL device is CPU, use the kernel with loops over clusters
            if self.device_type == cl.device_type.CPU:
                self.prg.normal_kd_logpost_loopy(self.queue, (self.obs.shape[0],), None,
                                                 d_labels, d_data, d_uniq_label, 
                                                 d_mu, d_n, d_determinants, d_inverses,
                                                 num_of_clusters, np.float32(self.alpha),
                                                 dim, wishart_v0, d_logpost.data, d_rand)
            # otherwise, use the kernel that fully unrolls data points and clusters
            else:
                self.prg.normal_kd_logpost(self.queue, (self.obs.shape[0], uniq_labels.shape[0]), None, 
                                           d_labels, d_data, d_uniq_label, 
                                           d_mu, d_n, d_determinants, d_inverses,
                                           num_of_clusters, np.float32(self.alpha),
                                           dim, wishart_v0, d_logpost.data, d_rand)
                self.prg.resample_labels(self.queue, (self.obs.shape[0],), None,
                                         d_labels, d_uniq_label, num_of_clusters,
                                         d_rand, d_logpost.data)

            cl.enqueue_copy(self.queue, cluster_labels, d_labels)
            gpu_time += time() - gpu_a_time
        
        total_time = time() - total_a_time
            
        return gpu_time, total_time, Counter(cluster_labels).most_common()


    def _logprob(self, sample):
        """Calculate the joint log probability of data and model given a sample.
        """
        assert(len(sample) == len(self.obs))

        try: dim = self.obs.shape[1]
        except IndexError: dim = 1
        
        total_logprob = 0

        if dim == 1 and self.cl_mode == False:
            cluster_dict = {}
            N = 0
            for label, obs in zip(sample, self.obs):
                obs = obs[0]
                if label in cluster_dict:
                    n = len(cluster_dict[label])
                    y_bar = np.mean(cluster_dict[label])
                    var = np.var(cluster_dict[label])
                else:
                    n, y_bar, var = 0, 0, 0
                    
                k_n = self.gaussian_k0 + n
                mu_n  = (self.gaussian_k0 * self.gaussian_mu0 + n * y_bar) / k_n
                alpha_n = self.gamma_alpha0 + n / 2
                beta_n = self.gamma_beta0 + 0.5 * var * n + \
                         self.gaussian_k0 * n * (y_bar - self.gaussian_mu0) ** 2 / (2 * k_n)
                Lambda = alpha_n * k_n / (beta_n * (k_n + 1))
                
                t_frozen = t(df = 2 * alpha_n, loc = mu_n, scale = (1 / Lambda) ** 0.5)
                loglik = t_frozen.logpdf(obs)
                loglik += np.log(n / (N + self.alpha)) if n > 0 else np.log(self.alpha / (N + self.alpha))

                # modify the counts and dict
                try: cluster_dict[label].append(obs)
                except KeyError: cluster_dict[label] = [obs]
                N += 1

                total_logprob += loglik

        if dim == 1 and self.cl_mode:
            gpu_a_time = time()
            d_data = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = self.obs[:,0])
            d_labels = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, hostbuf = sample)
            d_hyper_param = cl.Buffer(self.ctx, self.mf.READ_ONLY | self.mf.COPY_HOST_PTR, 
                                      hostbuf = np.array([self.gaussian_mu0, self.gaussian_k0, 
                                                          self.gamma_alpha0, self.gamma_beta0, self.alpha]).astype(np.float32))
            d_logprob = cl.array.empty(self.queue, (self.N,), np.float32)
            self.prg.joint_logprob(self.queue, self.obs.shape, None,
                                   d_labels, d_data, d_hyper_param, d_logprob.data)
            
            total_logprob = d_logprob.get().sum()
            self.gpu_time += time() - gpu_a_time
        return total_logprob
