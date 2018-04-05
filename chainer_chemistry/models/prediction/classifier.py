import warnings

import chainer
import numpy
from chainer.dataset.convert import concat_examples
from chainer.functions.evaluation import accuracy
from chainer.functions.loss import softmax_cross_entropy
from chainer import link, cuda
from chainer import reporter
from chainer.iterators import SerialIterator
from typing import Callable


def _to_tuple(x):
    if not isinstance(x, tuple):
        x = (x,)
    return x


def _extract_numpy(x):
    if isinstance(x, chainer.Variable):
        x = x.data
    return cuda.to_cpu(x)


def _argmax(*args):
    x = args[0]
    return chainer.functions.argmax(x, axis=1)


class Classifier(link.Chain):

    """A simple classifier model.

    This is an example of chain that wraps another chain. It computes the
    loss and accuracy based on a given input/label pair.

    Args:
        predictor (~chainer.Link): Predictor network.
        lossfun (function): Loss function.
        accfun (function): DEPRECATED. Please use `metrics_fun` instead.
        metrics_fun (function or dict or None): Function that computes metrics.
        label_key (int or str): Key to specify label variable from arguments.
            When it is ``int``, a variable in positional arguments is used.
            And when it is ``str``, a variable in keyword arguments is used.
        device (int): GPU device id of this Classifier to be used.
            -1 indicates to use in CPU.

    Attributes:
        predictor (~chainer.Link): Predictor network.
        lossfun (function): Loss function.
        accfun (function): DEPRECATED. Please use `metrics_fun` instead.
        y (~chainer.Variable): Prediction for the last minibatch.
        loss (~chainer.Variable): Loss value for the last minibatch.
        metrics (dict): Metrics computed in last minibatch
        compute_metrics (bool): If ``True``, compute metrics on the forward
            computation. The default value is ``True``.

    .. note::
        This link uses :func:`chainer.softmax_cross_entropy` with
        default arguments as a loss function (specified by ``lossfun``),
        if users do not explicitly change it. In particular, the loss function
        does not support double backpropagation.
        If you need second or higher order differentiation, you need to turn
        it on with ``enable_double_backprop=True``:

          >>> import chainer.functions as F
          >>> import chainer.links as L
          >>>
          >>> def lossfun(x, t):
          ...     return F.softmax_cross_entropy(
          ...         x, t, enable_double_backprop=True)
          >>>
          >>> predictor = L.Linear(10)
          >>> model = L.Classifier(predictor, lossfun=lossfun)

    """

    compute_metrics = True

    def __init__(self, predictor,
                 lossfun=softmax_cross_entropy.softmax_cross_entropy,
                 accfun=None, metrics_fun=accuracy.accuracy,
                 label_key=-1, device=-1):
        if not (isinstance(label_key, (int, str))):
            raise TypeError('label_key must be int or str, but is %s' %
                            type(label_key))
        if accfun is not None:
            warnings.warn(
                'accfun is deprecated, please use metrics_fun instead')
            warnings.warn('overriding metrics by accfun...')
            # override metrics by accfun
            metrics_fun = accfun

        super(Classifier, self).__init__()
        self.lossfun = lossfun
        if metrics_fun is None:
            self.compute_metrics = False
            self.metrics_fun = None
        elif isinstance(metrics_fun, Callable):
            self.metrics_fun = {'accuracy': metrics_fun}
        elif isinstance(metrics_fun, dict):
            self.metrics_fun = metrics_fun
        else:
            raise TypeError('Unexpected type metrics_fun must be None or '
                            'Callable or dict. actual {}'.format(type(accfun)))
        self.y = None
        self.loss = None
        self.metrics = None
        self.label_key = label_key

        with self.init_scope():
            self.predictor = predictor

        self.device = device
        if device >= 0:
            chainer.cuda.get_device_from_id(device).use()
            self.to_gpu()  # Copy the model to the GPU

    def __call__(self, *args, **kwargs):
        """Computes the loss value for an input and label pair.

        It also computes accuracy and stores it to the attribute.

        Args:
            args (list of ~chainer.Variable): Input minibatch.
            kwargs (dict of ~chainer.Variable): Input minibatch.

        When ``label_key`` is ``int``, the correpoding element in ``args``
        is treated as ground truth labels. And when it is ``str``, the
        element in ``kwargs`` is used.
        The all elements of ``args`` and ``kwargs`` except the ground trush
        labels are features.
        It feeds features to the predictor and compare the result
        with ground truth labels.

        Returns:
            ~chainer.Variable: Loss value.

        """

        # --- Separate `args` and `t` ---
        if isinstance(self.label_key, int):
            if not (-len(args) <= self.label_key < len(args)):
                msg = 'Label key %d is out of bounds' % self.label_key
                raise ValueError(msg)
            t = args[self.label_key]
            if self.label_key == -1:
                args = args[:-1]
            else:
                args = args[:self.label_key] + args[self.label_key + 1:]
        elif isinstance(self.label_key, str):
            if self.label_key not in kwargs:
                msg = 'Label key "%s" is not found' % self.label_key
                raise ValueError(msg)
            t = kwargs[self.label_key]
            del kwargs[self.label_key]
        else:
            raise TypeError('Label key type {} not supported'
                            .format(type(self.label_key)))

        self.y = None
        self.loss = None
        self.metrics = None
        self.y = self.predictor(*args, **kwargs)
        self.loss = self.lossfun(self.y, t)
        reporter.report({'loss': self.loss}, self)
        if self.compute_metrics:
            # Note: self.accuracy is `dict`, which is different from original
            # chainer implementation
            self.metrics = {key: value(self.y, t) for key, value in
                            self.metrics_fun.items()}
            reporter.report(self.metrics, self)
        return self.loss

    def _forward(self, data, fn, batchsize=16,
                 converter=concat_examples, retain_inputs=False,
                 preprocess_fn=None, postprocess_fn=None):
        """Forward data by iterating with batch

        Args:
            data: "train_x array" or "chainer dataset"
            fn (Callable): Main function to forward. Its input argument is
                either Variable, cupy.ndarray or numpy.ndarray, and returns
                Variable.
            batchsize (int): batch size
            converter (Callable): convert from `data` to `inputs`
            retain_inputs (bool): If True, this instance keeps inputs in 
                `self.inputs` or not.
            preprocess_fn (Callable): Its input is numpy.ndarray or 
                cupy.ndarray, it can return either Variable, cupy.ndarray or
                numpy.ndarray
            postprocess_fn (Callable): Its input argument is Variable,
                but this method may return either Variable, cupy.ndarray or
                numpy.ndarray.

        Returns (tuple or numpy.ndarray): forward result

        """
        input_list = None
        output_list = None
        it = SerialIterator(data, batch_size=batchsize, repeat=False,
                            shuffle=False)
        for batch in it:
            inputs = converter(batch, self.device)
            inputs = _to_tuple(inputs)

            if preprocess_fn:
                inputs = preprocess_fn(*inputs)
                inputs = _to_tuple(inputs)

            outputs = fn(*inputs)
            outputs = _to_tuple(outputs)

            # Init
            if retain_inputs:
                if input_list is None:
                    input_list = [[] for _ in range(len(inputs))]
                for j, input in enumerate(inputs):
                    input_list[j].append(cuda.to_cpu(input))
            if output_list is None:
                output_list = [[] for _ in range(len(outputs))]

            if postprocess_fn:
                outputs = postprocess_fn(*outputs)
                outputs = _to_tuple(outputs)
            for j, output in enumerate(outputs):
                output_list[j].append(_extract_numpy(output))

        if retain_inputs:
            self.inputs = [numpy.concatenate(in_array) for in_array in input_list]

        result = [numpy.concatenate(output) for output in output_list]
        if len(result) == 1:
            return result[0]
        else:
            return result

    def predict_proba(
            self, data, batchsize=16, converter=concat_examples,
            retain_inputs=False, preprocess_fn=None,
            postprocess_fn=chainer.functions.softmax):
        """Calculate probability of each category.

        Args:
            data: "train_x array" or "chainer dataset"
            fn (Callable): Main function to forward. Its input argument is
                either Variable, cupy.ndarray or numpy.ndarray, and returns
                Variable.
            batchsize (int): batch size
            converter (Callable): convert from `data` to `inputs`
            preprocess_fn (Callable): Its input is numpy.ndarray or 
                cupy.ndarray, it can return either Variable, cupy.ndarray or
                numpy.ndarray
            postprocess_fn (Callable): Its input argument is Variable,
                but this method may return either Variable, cupy.ndarray or
                numpy.ndarray.
            retain_inputs (bool): If True, this instance keeps inputs in 
                `self.inputs` or not.

        Returns (tuple or numpy.ndarray): Typically, it is 2-dimensional float
            array with shape (batchsize, number of category) which represents
            each examples probability to be each category.

        """
        with chainer.no_backprop_mode(), chainer.using_config('train', False):
            proba = self._forward(
                data, fn=self.predictor, batchsize=batchsize,
                converter=converter, retain_inputs=retain_inputs,
                preprocess_fn=preprocess_fn, postprocess_fn=postprocess_fn)
        return proba

    def predict(
            self, data, batchsize=16, converter=concat_examples,
            retain_inputs=False, preprocess_fn=None, postprocess_fn=_argmax):
        """Predict label of each category by taking .

        Args:
            data: 
            batchsize (int): batch size
            converter (Callable): convert from `data` to `inputs`
            preprocess_fn (Callable): Its input is numpy.ndarray or 
                cupy.ndarray, it can return either Variable, cupy.ndarray or
                numpy.ndarray
            postprocess_fn (Callable): Its input argument is Variable,
                but this method may return either Variable, cupy.ndarray or
                numpy.ndarray.
            retain_inputs (bool): If True, this instance keeps inputs in 
                `self.inputs` or not.

        Returns (tuple or numpy.ndarray): Typically, it is 1-dimensional int
            array with shape (batchsize, ) which represents each examples
            category prediction.

        """
        with chainer.no_backprop_mode(), chainer.using_config('train', False):
            predict_labels = self._forward(
                data, fn=self.predictor, batchsize=batchsize,
                converter=converter, retain_inputs=retain_inputs,
                preprocess_fn=preprocess_fn, postprocess_fn=postprocess_fn)
        return predict_labels

    # --- For backward compatibility ---
    @property
    def compute_accuracy(self):
        warnings.warn('compute_accuracy is deprecated,'
                      'please use compute_metrics instead')
        return self.compute_metrics

    @compute_accuracy.setter
    def compute_accuracy(self, value):
        warnings.warn('compute_accuracy is deprecated,'
                      'please use compute_metrics instead')
        self.compute_metrics = value

    @property
    def accuracy(self):
        warnings.warn('accuracy is deprecated,'
                      'please use metrics instead')
        return self.metrics

    @accuracy.setter
    def accuracy(self, value):
        warnings.warn('accuracy is deprecated,'
                      'please use metrics instead')
        self.metrics = value

    @property
    def accfun(self):
        warnings.warn('accfun is deprecated,'
                      'please use metrics_fun instead')
        return self.metrics_fun

    @accfun.setter
    def accfun(self, value):
        warnings.warn('accfun is deprecated,'
                      'please use metrics_fun instead')
        self.metrics_fun = value
