#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################
#  Copyright Kitware Inc.
#
#  Licensed under the Apache License, Version 2.0 ( the "License" );
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

import base64
import ctypes
import os

import six
try:
    from libtiff import libtiff_ctypes
except ImportError:
    # TODO: change print to use logger
    print 'Error: Could not import libtiff'
    # re-raise it for now, but maybe do something else in the future
    raise

from .cache import instanceLruCache


def patchLibtiff():
    libtiff_ctypes.libtiff.TIFFFieldWithTag.restype = ctypes.POINTER(libtiff_ctypes.TIFFFieldInfo)
    libtiff_ctypes.libtiff.TIFFFieldWithTag.argtypes = (libtiff_ctypes.TIFF, libtiff_ctypes.c_ttag_t)

    libtiff_ctypes.TIFFDataType.TIFF_LONG8 = 16  # BigTIFF 64-bit unsigned integer
    libtiff_ctypes.TIFFDataType.TIFF_SLONG8 = 17  # BigTIFF 64-bit signed integer
    libtiff_ctypes.TIFFDataType.TIFF_IFD8 = 18  # BigTIFF 64-bit unsigned integer (offset)
patchLibtiff()


class TiffException(Exception):
    pass


class InvalidOperationTiffException(TiffException):
    """
    An exception caused by the user making an invalid request of a TIFF file.
    """
    pass


class IOTiffException(TiffException):
    """
    An exception caused by an internal failure, due to an invalid file or other
    error.
    """
    pass


class ValidationTiffException(TiffException):
    """
    An exception caused by the TIFF reader not being able to support a given
    file.
    """
    pass


class TiledTiffDirectory(object):

    def __init__(self, filePath, directoryNum):
        """
        Create a new reader for a tiled image file directory in a TIFF file.

        :param filePath: A path to a TIFF file on disk.
        :type filePath: str
        :param directoryNum: The number of the TIFF image file directory to open.
        :type directoryNum: int
        :raises: InvalidOperationTiffException or IOTiffException or ValidationTiffException
        """
        self._tiffFile = None

        self._open(filePath, directoryNum)
        try:
            self._validate()
        except ValidationTiffException:
            self._close()
            raise
        self._loadMetadata()


    def __del__(self):
        self._close()


    def _open(self, filePath, directoryNum):
        """
        Open a TIFF file to a given file and IFD number.

        :param filePath: A path to a TIFF file on disk.
        :type filePath: str
        :param directoryNum: The number of the TIFF IFD to be used.
        :type directoryNum: int
        :raises: InvalidOperationTiffException or IOTiffException
        """
        self._close()
        if not os.path.isfile(filePath):
            raise InvalidOperationTiffException('TIFF file does not exist: ' % filePath)
        try:
            self._tiffFile = libtiff_ctypes.TIFF.open(filePath)
        except TypeError:
            raise IOTiffException('Could not open TIFF file: %s' % filePath)

        self._directoryNum = directoryNum
        if self._tiffFile.SetDirectory(self._directoryNum) != 1:
            self._tiffFile.close()
            raise IOTiffException('Could not set TIFF directory to %d' % directoryNum)


    def _close(self):
        if self._tiffFile:
            self._tiffFile.close()
            self._tiffFile = None


    def _validate(self):
        """
        Validate that this TIFF file and directory are suitable for reading.

        :raises: ValidationTiffException
        """
        if self._tiffFile.GetField('SamplesPerPixel') != 3:
            raise ValidationTiffException('Only RGB TIFF files are supported')

        if self._tiffFile.GetField('BitsPerSample') != 8:
            raise ValidationTiffException('Only single-byte sampled TIFF files are supported')

        if self._tiffFile.GetField('SampleFormat') not in (
                None,  # default is still SAMPLEFORMAT_UINT
                libtiff_ctypes.SAMPLEFORMAT_UINT):
            raise ValidationTiffException('Only unsigned int sampled TIFF files are supported')

        if self._tiffFile.GetField('PlanarConfig') != libtiff_ctypes.PLANARCONFIG_CONTIG:
            raise ValidationTiffException('Only contiguous planar configuration TIFF files are supported')

        if self._tiffFile.GetField('Photometric') not in (
                libtiff_ctypes.PHOTOMETRIC_RGB,
                libtiff_ctypes.PHOTOMETRIC_YCBCR):
            raise ValidationTiffException('Only RGB and YCbCr photometric interpretation TIFF files are supported')

        if self._tiffFile.GetField('Orientation') != libtiff_ctypes.ORIENTATION_TOPLEFT:
            raise ValidationTiffException('Only top-left orientation TIFF files are supported')

        if self._tiffFile.GetField('Compression') != libtiff_ctypes.COMPRESSION_JPEG:
            raise ValidationTiffException('Only JPEG compression TIFF files are supported')

        if not self._tiffFile.IsTiled():
            raise ValidationTiffException('Only tiled TIFF files are supported')

        if self._tiffFile.GetField('TileWidth') != self._tiffFile.GetField('TileLength'):
            raise ValidationTiffException('Non-square TIFF tiles are not supported')

        if self._tiffFile.GetField('JpegTablesMode') != \
                libtiff_ctypes.JPEGTABLESMODE_QUANT | libtiff_ctypes.JPEGTABLESMODE_HUFF:
            raise ValidationTiffException('Only TIFF files with separate Huffman and quantization tables are supported')


    def _loadMetadata(self):
        self._tileSize = self._tiffFile.GetField('TileWidth')
        self._imageWidth = self._tiffFile.GetField('ImageWidth')
        self._imageHeight = self._tiffFile.GetField('ImageLength')


    @instanceLruCache(1)
    def _getJpegTables(self):
        """
        Get the common JPEG Huffman-coding and quantization tables.

        See http://www.awaresystems.be/imaging/tiff/tifftags/jpegtables.html
        for more information.

        :return: All Huffman and quantization tables, with JPEG table start markers.
        :rtype: bytes
        :raises: Exception
        """
        # TODO: does this vary with Z?

        # TIFFTAG_JPEGTABLES uses (uint32*, void**) output arguments
        # http://www.remotesensing.org/libtiff/man/TIFFGetField.3tiff.html

        tableSize = ctypes.c_uint32()
        tableBuffer = ctypes.c_voidp()

        libtiff_ctypes.libtiff.TIFFGetField.argtypes = \
            libtiff_ctypes.libtiff.TIFFGetField.argtypes[:2] + \
            [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)]
        if libtiff_ctypes.libtiff.TIFFGetField(
                self._tiffFile,
                libtiff_ctypes.TIFFTAG_JPEGTABLES,
                ctypes.byref(tableSize),
                ctypes.byref(tableBuffer)) != 1:
            raise IOTiffException('Could not get JPEG Huffman / quantization tables')

        tableSize = tableSize.value
        tableBuffer = ctypes.cast(tableBuffer, ctypes.POINTER(ctypes.c_char))

        if tableBuffer[:2] != b'\xff\xd8':
            raise IOTiffException('Missing JPEG Start Of Image marker in tables')
        if tableBuffer[tableSize - 2:tableSize] != b'\xff\xd9':
            raise IOTiffException('Missing JPEG End Of Image marker in tables')
        if tableBuffer[2:4] not in (b'\xff\xc4', b'\xff\xdb'):
            raise IOTiffException('Missing JPEG Huffman or Quantization Table marker')

        # Strip the Start / End Of Image markers
        tableData = tableBuffer[2:tableSize - 2]
        return tableData


    def _toTileNum(self, x, y):
        """
        Get the internal tile number of a tile, from its row and column index.

        :param x: The column index of the desired tile.
        :type x: int
        :param y: The row index of the desired tile.
        :type y: int
        :return: The internal tile number of the desired tile.
        :rtype int
        :raises: InvalidOperationTiffException
        """
        # TODO: is it worth it to memoize this?

        # TIFFCheckTile and TIFFComputeTile require pixel coordinates
        pixelX = x * self._tileSize
        pixelY = y * self._tileSize

        if libtiff_ctypes.libtiff.TIFFCheckTile(
                self._tiffFile, pixelX, pixelY, 0, 0) == 0:
            raise InvalidOperationTiffException('Tile x=%d, y=%d does not exist' % (x, y))

        tileNum = libtiff_ctypes.libtiff.TIFFComputeTile(
            self._tiffFile, pixelX, pixelY, 0, 0).value
        return tileNum


    @instanceLruCache(1)
    def _getTileByteCountsType(self):
        """
        Get data type of the elements in the TIFFTAG_TILEBYTECOUNTS array.

        :return: The element type in TIFFTAG_TILEBYTECOUNTS.
        :rtype: ctypes.c_uint64 or ctypes.c_uint16
        :raises: IOTiffException
        """
        tileByteCountsFieldInfo = libtiff_ctypes.libtiff.TIFFFieldWithTag(
            self._tiffFile, libtiff_ctypes.TIFFTAG_TILEBYTECOUNTS).contents
        tileByteCountsLibtiffType = tileByteCountsFieldInfo.field_type

        if tileByteCountsLibtiffType == libtiff_ctypes.TIFFDataType.TIFF_LONG8:
            return ctypes.c_uint64
        elif tileByteCountsLibtiffType == libtiff_ctypes.TIFFDataType.TIFF_SHORT:
            return ctypes.c_uint16
        else:
            raise IOTiffException('Invalid type for TIFFTAG_TILEBYTECOUNTS: %s' %
                                  tileByteCountsLibtiffType)


    def _getJpegFrameSize(self, tileNum):
        """
        Get the file size in bytes of the raw encoded JPEG frame for a tile.

        :param tileNum: The internal tile number of the desired tile.
        :type tileNum: int
        :return: The size in bytes of the raw tile data for the desired tile.
        :rtype: int
        :raises: InvalidOperationTiffException or IOTiffException
        """
        # TODO: is it worth it to memoize this?

        # TODO: remove this check, for additional speed
        totalTileCount = libtiff_ctypes.libtiff.TIFFNumberOfTiles(self._tiffFile).value
        if tileNum >= totalTileCount:
            raise InvalidOperationTiffException('Tile number out of range')

        # pylibtiff treats the output of TIFFTAG_TILEBYTECOUNTS as a scalar
        # uint32; libtiff's documentation specifies that the output will be an
        # array of uint32; in reality and per the TIFF spec, the output is an
        # array of either uint64 or unit16, so we need to call the ctypes
        # interface directly to get this tag
        # http://www.awaresystems.be/imaging/tiff/tifftags/tilebytecounts.html

        rawTileSizesType = self._getTileByteCountsType()
        rawTileSizes = ctypes.POINTER(rawTileSizesType)()

        libtiff_ctypes.libtiff.TIFFGetField.argtypes = \
            libtiff_ctypes.libtiff.TIFFGetField.argtypes[:2] + \
            [ctypes.POINTER(ctypes.POINTER(rawTileSizesType))]
        if libtiff_ctypes.libtiff.TIFFGetField(
                self._tiffFile,
                libtiff_ctypes.TIFFTAG_TILEBYTECOUNTS,
                ctypes.byref(rawTileSizes)) != 1:
            raise IOTiffException('Could not get raw tile size')

        # In practice, this will never overflow, and it's simpler to convert the
        # long to an int
        return int(rawTileSizes[tileNum])


    def _getJpegFrame(self, tileNum):
        """
        Get the raw encoded JPEG image frame from a tile.

        :param tileNum: The internal tile number of the desired tile.
        :type tileNum: int
        :return: The JPEG image frame, including a JPEG Start Of Frame marker.
        :rtype: bytes
        :raises: InvalidOperationTiffException or IOTiffException
        """
        # This raises an InvalidOperationTiffException if the tile doesn't exist
        rawTileSize = self._getJpegFrameSize(tileNum)

        frameBuffer = ctypes.create_string_buffer(rawTileSize)

        bytesRead = libtiff_ctypes.libtiff.TIFFReadRawTile(
            self._tiffFile, tileNum,
            frameBuffer, rawTileSize).value
        if bytesRead == -1:
            raise IOTiffException('Failed to read raw tile')
        elif bytesRead < rawTileSize:
            raise IOTiffException('Buffer underflow when reading tile')
        elif bytesRead > rawTileSize:
            # It's unlikely that this will ever occur, but incomplete reads will
            # be checked for by looking for the JPEG end marker
            raise IOTiffException('Buffer overflow when reading tile')

        if frameBuffer.raw[:2] != b'\xff\xd8':
            raise IOTiffException('Missing JPEG Start Of Image marker in frame')
        if frameBuffer.raw[-2:] != b'\xff\xd9':
            raise IOTiffException('Missing JPEG End Of Image marker in frame')
        if frameBuffer.raw[2:4] in (b'\xff\xc0', b'\xff\xc2'):
            frameStartPos = 2
        else:
            # VIPS may encode TIFFs with the quantization (but not Huffman)
            # tables also at the start of every frame, so locate them for
            # removal
            # VIPS seems to prefer Baseline DCT, so search for that first
            frameStartPos = frameBuffer.raw.find(b'\xff\xc0', 2, -2)
            if frameStartPos == -1:
                frameStartPos = frameBuffer.raw.find(b'\xff\xc2', 2, -2)
                if frameStartPos == -1:
                    raise IOTiffException('Missing JPEG Start Of Frame marker')

        # Strip the Start / End Of Image markers
        tileData = frameBuffer.raw[frameStartPos:-2]
        return tileData


    @property
    def tileSize(self):
        """
        Get the pixel size of tiles.

        :return: The tile size (length and height) in pixels.
        :rtype: int
        """
        # TODO: fetch lazily and memoize
        return self._tileSize


    @property
    def imageWidth(self):
        # TODO: fetch lazily and memoize
        return self._imageWidth


    @property
    def imageHeight(self):
        # TODO: fetch lazily and memoize
        return self._imageHeight


    def getTile(self, x, y):
        """
        Get the complete JPEG image from a tile.

        :param x: The column index of the desired tile.
        :type x: int
        :param y: The row index of the desired tile.
        :type y: int
        :rtype: bytes
        :raises: InvalidOperationTiffException or IOTiffException
        """
        # This raises an InvalidOperationTiffException if the tile doesn't exist
        tileNum = self._toTileNum(x, y)

        imageBuffer = six.BytesIO()

        # Write JPEG Start Of Image marker
        imageBuffer.write(b'\xff\xd8')

        imageBuffer.write(self._getJpegTables())

        # TODO: why write padding?
        imageBuffer.write(b'\xff\xff\xff\xff')

        imageBuffer.write(self._getJpegFrame(tileNum))

        # Write JPEG End Of Image marker
        imageBuffer.write(b'\xff\xd9')

        return imageBuffer.getvalue()


    # TODO: refactor and remove this
    def parse_image_description(self):
        from xml.etree import cElementTree as ET
        import logging
        logger = logging.getLogger('slideatlas')

        self.levels = {}
        self.isBigTIFF = False
        self.barcode = ""
        self.tif = None

        self.meta = self.tif.GetField("ImageDescription")

        if self.meta == None:
            # Missing meta information (typical of zeiss files)
            # Verify that the levels exist
            logger.warning('No ImageDescription in file')
            return

        try:
            xml = ET.fromstring(self.meta)

            # Parse the string for BigTIFF format
            descstr = xml.find(
                ".//*[@Name='DICOM_DERIVATION_DESCRIPTION']").text
            if descstr.find("useBigTIFF=1") > 0:
                self.isBigTIFF = True

            # Parse the barcode string
            self.barcode = base64.b64decode(
                xml.find(".//*[@Name='PIM_DP_UFS_BARCODE']").text)
            # self.barcode["words"] = self.barcode["str"].split("|")
            # self.barcode["physician_id"],  self.barcode["case_id"]= self.barcode["words"][0].split(" ")
            # self.barcode["stain_id"] = self.barcode["words"][4]

            logger.debug(self.barcode)

            # Parse the attribute named "DICOM_DERIVATION_DESCRIPTION"
            # tiff-useBigTIFF=1-clip=2-gain=10-useRgb=0-levels=10003,10002,10000,10001-q75;PHILIPS
            # UFS V1.6.5574
            descstr = xml.find(
                ".//*[@Name='DICOM_DERIVATION_DESCRIPTION']").text
            if descstr.find("useBigTIFF=1") > 0:
                self.isBigTIFF = True

            # logger.debug(descstr)

            for b in xml.findall(".//DataObject[@ObjectType='PixelDataRepresentation']"):
                level = int(
                    b.find(".//*[@Name='PIIM_PIXEL_DATA_REPRESENTATION_NUMBER']").text)
                columns = int(
                    b.find(".//*[@Name='PIIM_PIXEL_DATA_REPRESENTATION_COLUMNS']").text)
                rows = int(
                    b.find(".//*[@Name='PIIM_PIXEL_DATA_REPRESENTATION_ROWS']").text)
                self.levels[level] = [columns, rows]

            self.embedded_images = {}
            # Extract macro and label images
            for animage in xml.findall(".//*[@ObjectType='DPScannedImage']"):
                typestr = animage.find(".//*[@Name='PIM_DP_IMAGE_TYPE']").text
                if typestr == "LABELIMAGE":
                    self.embedded_images["label"] = animage.find(
                        ".//*[@Name='PIM_DP_IMAGE_DATA']").text
                    pass
                elif typestr == "MACROIMAGE":
                    self.embedded_images["macro"] = animage.find(
                        ".//*[@Name='PIM_DP_IMAGE_DATA']").text
                    pass
                elif typestr == "WSI":
                    pass
                else:
                    logger.error('Unforeseen embedded image: %s', typestr)

                #columns = int(b.find(".//*[@Name='PIIM_PIXEL_DATA_REPRESENTATION_COLUMNS']").text)

            if descstr.find("useBigTIFF=1") > 0:
                self.isBigTIFF = True

        except Exception as E:
            logger.warning('Image Description failed for valid Philips XML because %s', E.message)