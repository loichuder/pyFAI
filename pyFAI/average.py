#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
#    Project: Fast Azimuthal integration
#             https://github.com/pyFAI/pyFAI
#
#    Copyright (C) European Synchrotron Radiation Facility, Grenoble, France
#
#    Principal author:       Jérôme Kieffer (Jerome.Kieffer@ESRF.eu)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

"""

Utilities, mainly for image treatment

"""

__author__ = "Jerome Kieffer"
__contact__ = "Jerome.Kieffer@ESRF.eu"
__license__ = "GPLv3+"
__copyright__ = "European Synchrotron Radiation Facility, Grenoble, France"
__date__ = "08/08/2016"
__status__ = "production"

import logging
import numpy
import fabio
import weakref

try:
    from .third_party import six
except (ImportError, Exception):
    import six

from .utils import removeSaturatedPixel
from .utils import exists

from ._version import calc_hexversion
if ("hexversion" not in dir(fabio)) or (fabio.hexversion < calc_hexversion(0, 4, 0, "dev", 5)):
    # Short cut fabio.factory do not exists on older versions
    fabio.factory = fabio.fabioimage.FabioImage.factory

logger = logging.getLogger("pyFAI.average")


class ImageReductionFilter(object):
    """
    Generic filter applyed in a set of images.
    """

    def init(self, max_images=None):
        """
        Initialize the filter before using it.

        @param max_images int: Max images supported by the filter
        """
        pass

    def add_image(self, image):
        """
        Add an image to the filter.

        @param image numpy.ndarray: image to add
        """
        raise NotImplementedError()

    def get_parameters(self):
        """Return a dictionary containing filter parameters"""
        return {"cutoff": None, "quantiles": None}

    def get_result(self):
        """
        Get the result of the filter.

        @return: result filter
        """
        raise NotImplementedError()


class ImageAccumulatorFilter(ImageReductionFilter):
    """
    Filter applyed in a set of images in which it is possible
    to reduce data step by step into a single merged image.
    """

    def init(self, max_images=None):
        self._count = 0
        self._accumulated_image = None

    def add_image(self, image):
        """
        Add an image to the filter.

        @param image numpy.ndarray: image to add
        """
        self._accumulated_image = self._accumulate(self._accumulated_image, image)
        self._count += 1

    def _accumulate(self, accumulated_image, added_image):
        """
        Add an image to the filter.

        @param accumulated_image numpy.ndarray: image use to accumulate information
        @param added_image numpy.ndarray: image to add
        """
        raise NotImplementedError()

    def get_result(self):
        """
        Get the result of the filter.

        @return: result filter
        @rtype: numpy.ndarray
        """
        return self._accumulated_image


class MaxAveraging(ImageAccumulatorFilter):
    name = "max"

    def _accumulate(self, accumulated_image, added_image):
        if accumulated_image is None:
            return added_image
        return numpy.maximum(accumulated_image, added_image)


class MinAveraging(ImageAccumulatorFilter):
    name = "min"

    def _accumulate(self, accumulated_image, added_image):
        if accumulated_image is None:
            return added_image
        return numpy.minimum(accumulated_image, added_image)


class SumAveraging(ImageAccumulatorFilter):
    name = "sum"

    def _accumulate(self, accumulated_image, added_image):
        if accumulated_image is None:
            return added_image
        return accumulated_image + added_image


class MeanAveraging(SumAveraging):
    name = "mean"

    def get_result(self):
        return self._accumulated_image / numpy.float32(self._count)


class ImageStackFilter(ImageReductionFilter):
    """
    Filter creating a stack from all images and computing everything at the end.
    """
    def init(self, max_images=None):
        self._stack = None
        self._max_stack_size = max_images
        self._count = 0

    def add_image(self, image):
        """
        Add an image to the filter.

        @param image numpy.ndarray: image to add
        """
        if self._stack is None:
            shape = self._max_stack_size, image.shape[0], image.shape[1]
            self._stack = numpy.zeros(shape, dtype=numpy.float32)
        self._stack[self._count] = image
        self._count += 1

    def _compute_stack_reduction(self, stack):
        raise NotImplementedError()

    def get_result(self):
        if self._stack is None:
            raise Exception("No data to reduce")

        shape = self._count, self._stack.shape[1], self._stack.shape[2]
        self._stack.resize(shape)
        return self._compute_stack_reduction(self._stack)


class AverageDarkFilter(ImageStackFilter):
    """
    Filter based on the algorithm of average_dark

    TODO: Must be splited according to each filter_name, and removed
    """
    def __init__(self, filter_name, cut_off, quantiles):
        super(AverageDarkFilter, self).__init__()
        self._filter_name = filter_name
        self._cut_off = cut_off
        self._quantiles = quantiles

    @property
    def name(self):
        return self._filter_name

    def get_parameters(self):
        """Return a dictionary containing filter parameters"""
        return {"cutoff": self._cut_off, "quantiles": self._quantiles}

    def _compute_stack_reduction(self, stack):
        """
        Compute the stack reduction.

        @param stack numpy.ndarray: stack to reduce

        @return: result filter
        @rtype: numpy.ndarray
        """
        return average_dark(stack,
            self._filter_name,
            self._cut_off,
            self._quantiles)


_FILTERS = [
    MaxAveraging,
    MinAveraging,
    MeanAveraging,
    SumAveraging,
]

_FILTER_NAME_MAPPING = {}
for f in _FILTERS:
    _FILTER_NAME_MAPPING[f.name] = f


def _get_filter_class(filter_name):
    global _FILTER_NAME_MAPPING
    filter_class = _FILTER_NAME_MAPPING.get(filter_name, None)
    if filter_class is None:
        raise Exception("Filter name '%s' unknown" % filter_name)
    return filter_class


def average_dark(lstimg, center_method="mean", cutoff=None, quantiles=(0.5, 0.5)):
    """
    Averages a serie of dark (or flat) images.
    Centers the result on the mean or the median ...
    but averages all frames within  cutoff*std

    @param lstimg: list of 2D images or a 3D stack
    @param center_method: is the center calculated by a "mean", "median", "quantile", "std"
    @param cutoff: keep all data where (I-center)/std < cutoff
    @param quantiles: 2-tuple of floats average out data between the two quantiles

    @return: 2D image averaged
    """
    if "ndim" in dir(lstimg) and lstimg.ndim == 3:
        stack = lstimg.astype(numpy.float32)
        shape = stack.shape[1:]
        length = stack.shape[0]
    else:
        shape = lstimg[0].shape
        length = len(lstimg)
        if length == 1:
            return lstimg[0].astype(numpy.float32)
        stack = numpy.zeros((length, shape[0], shape[1]), dtype=numpy.float32)
        for i, img in enumerate(lstimg):
            stack[i] = img
    if center_method in dir(stack):
        center = stack.__getattribute__(center_method)(axis=0)
    elif center_method == "median":
        logger.info("Filtering data (median)")
        center = numpy.median(stack, axis=0)
    elif center_method.startswith("quantil"):
        logger.info("Filtering data (quantiles: %s)", quantiles)
        sorted_ = numpy.sort(stack, axis=0)
        lower = max(0, int(numpy.floor(min(quantiles) * length)))
        upper = min(length, int(numpy.ceil(max(quantiles) * length)))
        if (upper == lower):
            if upper < length:
                upper += 1
            elif lower > 0:
                lower -= 1
            else:
                logger.warning("Empty selection for quantil %s, would keep points from %s to %s", quantiles, lower, upper)
        center = sorted_[lower:upper].mean(axis=0)
    else:
        raise RuntimeError("Cannot understand method: %s in average_dark" % center_method)
    if cutoff is None or cutoff <= 0:
        output = center
    else:
        std = stack.std(axis=0)
        strides = 0, std.strides[0], std.strides[1]
        std.shape = 1, shape[0], shape[1]
        std.strides = strides
        center.shape = 1, shape[0], shape[1]
        center.strides = strides
        mask = ((abs(stack - center) / std) > cutoff)
        stack[numpy.where(mask)] = 0.0
        summed = stack.sum(axis=0)
        output = summed / numpy.maximum(1, (length - mask.sum(axis=0)))
    return output


class MonitorNotFound(Exception):
    """Raised when monitor information in not found or is not valid."""
    pass


def _get_monitor_value_from_edf(image, monitor_key):
    """Return the monitor value from an EDF image using an header key.

    Take care of the counter and motor syntax using for example 'counter/bmon'
    which reach 'bmon' value from 'counter_pos' key using index from
    'counter_mne' key.

    @param image fabio.fabioimage.FabioImage: Image containing the header
    @param monitor_key str: Key containing the monitor
    @return: returns the monitor else raise a MonitorNotFound
    @rtype: float
    @raise MonitorNotFound: when the expected monitor is not found on the header
    """
    keys = image.header

    if "/" in monitor_key:
        base_key, mnemonic = monitor_key.split('/', 1)

        mnemonic_values_key = base_key + "_mne"
        mnemonic_values =  keys.get(mnemonic_values_key, None)
        if mnemonic_values is None:
            raise MonitorNotFound("Monitor mnemonic key '%s' not found in the header" % (mnemonic_values_key))

        mnemonic_values = mnemonic_values.split()
        pos_values_key = base_key + "_pos"
        pos_values =  keys.get(pos_values_key)
        if pos_values is None:
            raise MonitorNotFound("Monitor pos key '%s' not found in the header" % (pos_values_key))

        pos_values = pos_values.split()

        try:
            index = mnemonic_values.index(mnemonic)
        except ValueError as _e:
            logger.debug("Exception", exc_info=1)
            raise MonitorNotFound("Monitor mnemonic '%s' not found in the header key '%s'" % (mnemonic, mnemonic_values_key))

        if index >= len(pos_values):
            raise MonitorNotFound("Monitor value '%s' not found in '%s'. Not enougth values." % (pos_values_key))

        monitor = pos_values[index]

    else:
        if monitor_key not in keys:
            raise MonitorNotFound("Monitor key '%s' not found in the header" % (monitor_key))
        monitor = keys[monitor_key]

    try:
        monitor = float(monitor)
    except ValueError as _e:
        logger.debug("Exception", exc_info=1)
        raise MonitorNotFound("Monitor value '%s' is not valid" % (monitor))
    return monitor


def _get_monitor_value(image, monitor_key):
    """Return the monitor value from an image using an header key.

    @param image fabio.fabioimage.FabioImage: Image containing the header
    @param monitor_key str: Key containing the monitor
    @return: returns the monitor else raise an exception
    @rtype: float
    @raise MonitorNotFound: when the expected monitor is not found on the header
    """
    if monitor_key is None:
        return Exception("No monitor defined")

    if isinstance(image, fabio.edfimage.EdfImage):
        return _get_monitor_value_from_edf(image, monitor_key)
    elif isinstance(image, fabio.numpyimage.numpyimage):
        return _get_monitor_value_from_edf(image, monitor_key)
    else:
        raise Exception("File format '%s' unsupported" % type(image))


def _normalize_image_stack(image_stack):
    """
    Convert input data to a list of 2D numpy arrays or a stack
    of numpy array (3D array).

    @param image_stack list or numpy.ndarray: slice of images
    @return: A stack of image (list of 2D array or a single 3D array)
    @rtype: list or numpy.ndarray
    """
    if image_stack is None:
        return None

    if isinstance(image_stack, numpy.ndarray) and image_stack.ndim == 3:
        # numpy image stack (single 3D image)
        return image_stack

    if isinstance(image_stack, list):
        # list of numpy images (multi 2D images)
        result = []
        for image in image_stack:
            if isinstance(image, six.string_types):
                data = fabio.open(image).data
            elif isinstance(image, numpy.ndarray) and image.ndim == 2:
                data = image
            else:
                raise Exception("Unsupported image type '%s' in image_stack" % type(image))
            result.append(data)
        return result

    raise Exception("Unsupported type '%s' for image_stack" % type(image_stack))


class AverageWriter():

    def write_header(self, merged_files, nb_frames):
        raise NotImplementedError()

    def write_reduction(self, reduction):
        raise NotImplementedError()

    def close(self):
        """Close the writer. Must not be used anymore."""
        raise NotImplementedError()


class MultiFilesAverageWriter(AverageWriter):
    """Write reductions into multi files. File headers are duplicated."""

    def __init__(self, file_name_pattern, file_format, dry_run=False):
        """
        @param file_name_pattern str: File name pattern for the output files.
            If it contains "%(reduction_name)s", it is updated for each
            reduction writing with the name of the reduction.
        @param file_format str: File format used. It is the default
            extension file.
        @param dry_run bool: If dry_run, the file is created on memory but not
            saved on the file system at the end
        """
        self._file_name_pattern = file_name_pattern
        self._global_header = {}
        self._fabio_images = weakref.WeakKeyDictionary()
        self._dry_run = dry_run

        # in case "edf.gz"
        if "." in file_format:
            file_format = file_format.split(".")[0]

        self._fabio_class = fabio.factory(file_format + "image")

    def write_header(self, merged_files, nb_frames):
        self._global_header["nfiles"] = len(merged_files)
        self._global_header["nframes"] = nb_frames

        pattern = "merged_file_%%0%ii" % len(str(len(merged_files)))
        for i, f in enumerate(merged_files):
            name = pattern % i
            self._global_header[name] = f.filename

    def _get_file_name(self, reduction_name):
        keys = {"reduction_name": reduction_name}
        return self._file_name_pattern % keys

    def write_reduction(self, algorithm, data):
        file_name = self._get_file_name(algorithm.name)
        # overwrite the method
        header = fabio.fabioimage.OrderedDict()
        header["method"] = algorithm.name
        for name, value in self._global_header.items():
            header[name] = str(value)
        filter_parameters = algorithm.get_parameters()
        for name, value in filter_parameters.items():
            header[name] = str(value)
        image = self._fabio_class.__class__(data=data, header=header)
        if not self._dry_run:
            image.write(file_name)
            logger.info("Wrote %s", file_name)
        self._fabio_images[algorithm] = image

    def get_fabio_image(self, algorithm):
        """Get the constructed fabio image

        @rtype: fabio.fabioimage.FabioImage
        """
        return self._fabio_images[algorithm]

    def close(self):
        """Close the writer. Must not be used anymore."""
        self._header = None


def common_prefix(string_list):
    """Return the common prefix of a list of strings

    TODO: move it into utils package

    @param string_list list of str: List of strings
    @rtype: str
    """
    prefix = ""
    for ch in zip(string_list):
        c = ch[0]
        good = True
        for i in ch:
            if i != c:
                good = False
                break
        if good:
            prefix += c
        else:
            break
    return prefix


class Average(object):

    def __init__(self):
        self._dark = None
        self._raw_flat = None
        self._flat = None
        self._monitor_key = None
        self._threshold = None
        self._minimum = None
        self._maximum = None
        self._fabio_images = []
        self._writer = None
        self._algorithms = []
        self._nb_frames = 0
        self._correct_flat_from_dark = False
        self._results = weakref.WeakKeyDictionary()

    def set_dark(self, dark_list):
        if dark_list is None:
            self._dark = None
            return
        darks = _normalize_image_stack(dark_list)
        self._dark = average_dark(darks, center_method="mean", cutoff=4)

    def set_flat(self, flat_list):
        if flat_list is None:
            self._raw_flat = None
            return
        flats = _normalize_image_stack(flat_list)
        self._raw_flat = average_dark(flats, center_method="mean", cutoff=4)

    def set_correct_flat_from_dark(self, correct_flat_from_dark):
        self._correct_flat_from_dark = correct_flat_from_dark

    def get_counter_frames(self):
        return self._nb_frames

    def get_fabio_imnges(self):
        return self._fabio_images

    def set_images(self, image_list):
        self._fabio_images = []
        self._nb_frames = 0
        for image in image_list:
            if isinstance(image, six.string_types):
                logger.info("Reading %s", image)
                fabio_image = fabio.open(image)
            elif isinstance(image, fabio.fabioimage.fabioimage):
                fabio_image = image
            else:
                if fabio.hexversion < 262148:
                    logger.error("Old version of fabio detected, upgrade to 0.4 or newer")

                # Assume this is a numpy array like
                if not isinstance(image, numpy.ndarray):
                    raise RuntimeError("Not good type for input, got %s, expected numpy array" % type(image))
                fabio_image = fabio.numpyimage.NumpyImage(data=image)

            self._fabio_images.append(fabio_image)
            self._nb_frames += fabio_image.nframes

    def set_monitor_name(self, monitor_name):
        self._monitor_key = monitor_name

    def set_pixel_filter(self, threshold, minimum, maximum):
        self._threshold = threshold
        self._minimum = minimum
        self._maximum = maximum

    def set_writer(self, writer):
        self._writer = writer

    def add_algorithm(self, algorithm):
        self._algorithms.append(algorithm)

    def _get_corrected_image(self, fabio_image, image):
        "internal subfunction for dark/flat/monitor "
        corrected_image = numpy.ascontiguousarray(image, numpy.float32)
        if self._threshold or self._minimum or self._maximum:
            corrected_image = removeSaturatedPixel(corrected_image, self._threshold, self._minimum, self._maximum)
        if self._dark is not None:
            corrected_image -= self._dark
        if self._flat is not None:
            corrected_image /= self._flat
        if self._monitor_key is not None:
            try:
                monitor = _get_monitor_value(fabio_image, self._monitor_key)
                corrected_image /= monitor
            except MonitorNotFound as e:
                logger.warning("Monitor not found in filename '%s', data skipped. Cause: %s", fabio_image.filename, str(e))
                return None
        return corrected_image

    def _get_image_reduction(self, algorithm):
        algorithm.init(max_images=self._nb_frames)
        for fabio_image in self._fabio_images:
            for frame in range(fabio_image.nframes):
                if fabio_image.nframes == 1:
                    data = fabio_image.data
                else:
                    data = fabio_image.getframe(frame).data
                logger.debug("Intensity range for %s#%i is %s --> %s", fabio_image.filename, frame, data.min(), data.max())

                corrected_image = self._get_corrected_image(fabio_image, data)
                if corrected_image is None:
                    continue
                algorithm.add_image(corrected_image)
        return algorithm.get_result()

    def _update_flat(self):
        """
        Update the flat according to the last process parameters
        """
        if self._raw_flat is not None:
            flat = numpy.array(self._raw_flat)
            if self._correct_flat_from_dark:
                if self._dark is not None:
                    flat -= self._dark
                else:
                    logger.debug("No dark. Flat correction using dark skipped")
            flat[numpy.where(flat <= 0)] = 1.0
        else:
            flat = None
        self._flat = flat

    def process(self):
        self._update_flat()
        writer = self._writer

        if writer is not None:
            writer.write_header(self._fabio_images, self._nb_frames)

        for algorithm in self._algorithms:
            image_reduction = self._get_image_reduction(algorithm)
            logger.debug("Intensity range in merged dataset : %s --> %s", image_reduction.min(), image_reduction.max())
            if writer is not None:
                writer.write_reduction(algorithm, image_reduction)
            self._results[algorithm] = image_reduction

        if writer is not None:
            writer.close()

    def get_image_reduction(self, algorithm):
        return self._results[algorithm]


def average_images(listImages, output=None, threshold=0.1, minimum=None, maximum=None,
                  darks=None, flats=None, filter_="mean", correct_flat_from_dark=False,
                  cutoff=None, quantiles=None, fformat="edf", monitor_key=None):
    """
    Takes a list of filenames and create an average frame discarding all saturated pixels.

    @param listImages: list of string representing the filenames
    @param output: name of the optional output file
    @param threshold: what is the upper limit? all pixel > max*(1-threshold) are discareded.
    @param minimum: minimum valid value or True
    @param maximum: maximum valid value
    @param darks: list of dark current images for subtraction
    @param flats: list of flat field images for division
    @param filter_: can be "min", "max", "median", "mean", "sum", "quantiles" (default='mean')
    @param correct_flat_from_dark: shall the flat be re-corrected ?
    @param cutoff: keep all data where (I-center)/std < cutoff
    @param quantiles: 2-tuple containing the lower and upper quantile (0<q<1) to average out.
    @param fformat: file format of the output image, default: edf
    @param monitor_key str: Key containing the monitor. Can be none.
    @return: filename with the data or the data ndarray in case format=None
    """

    # input sanitization
    if filter_ not in ["min", "max", "median", "mean", "sum", "quantiles", "std"]:
        logger.warning("Filter %s not understood. switch to mean filter", filter_)
        filter_ = "mean"

    average = Average()
    average.set_images(listImages)
    average.set_dark(darks)
    average.set_flat(flats)
    average.set_correct_flat_from_dark(correct_flat_from_dark)
    average.set_monitor_name(monitor_key)
    average.set_pixel_filter(threshold, minimum, maximum)

    # define reduction algorithm according to params
    if (cutoff or quantiles or (filter_ in ["median", "quantiles", "std"])):
        algorithm = AverageDarkFilter(filter_, cutoff, quantiles)
    else:
        filter_class = _get_filter_class(filter_)
        algorithm = filter_class()
    average.add_algorithm(algorithm)

    # define writer
    if fformat is not None:
        if fformat.startswith("."):
            fformat = fformat.lstrip(".")
        if output is None:
            prefix = common_prefix([i.filename for i in average.get_fabio_images()])
            output = "filt%02i-%s.%s" % (average.get_counter_frames(), prefix, fformat)
            output = "%(reduction_name)s" + output

    if output is not None:
        writer = MultiFilesAverageWriter(output, fformat)
        average.set_writer(writer)
    else:
        writer = None

    average.process()

    if writer is not None:
        fabio_image = writer.get_fabio_image(algorithm)
        return fabio_image.filename
    else:
        return average.get_image_reduction(algorithm)
