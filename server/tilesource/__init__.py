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

# Deal with a bug where PEP257 crashes when parsing __all__
# flake8: noqa

import collections
import sys
from .base import TileSource, TileSourceException, \
    TileSourceAssetstoreException
try:
    import girder
    from girder.constants import TerminalColor
    from girder import logger
    from .base import GirderTileSource
except ImportError:
    import logging as logger
    girder = None


AvailableTileSources = collections.OrderedDict()
all = [TileSource, TileSourceException, TileSourceAssetstoreException,
       AvailableTileSources]

if girder:
    all.append(GirderTileSource)

sourceList = [
    {'moduleName': '.tiff', 'className': 'TiffFileTileSource'},
    {'moduleName': '.tiff', 'className': 'TiffGirderTileSource',
     'girder': True},
    {'moduleName': '.svs', 'className': 'SVSFileTileSource'},
    {'moduleName': '.svs', 'className': 'SVSGirderTileSource', 'girder': True},
    {'moduleName': '.test', 'className': 'TestTileSource'},
    {'moduleName': '.dummy', 'className': 'DummyTileSource'},
]
for source in sourceList:
    try:
        # Don't try to load girder sources if we couldn't import girder
        if not girder and source.get('girder'):
            continue
        # For each of our sources, try to import the named class from the
        # source module
        className = source['className']
        sourceModule = __import__(
            source['moduleName'].lstrip('.'), globals(), locals(), [className],
            len(source['moduleName']) - len(source['moduleName'].lstrip('.')))
        sourceClass = getattr(sourceModule, className)
        # Add the source class to the locals name so that it can be reached by
        # importing the tilesource module
        locals().update({className: sourceClass})
        # add it to our list of exports
        all.append(sourceClass)
        # add it to our dictionary of available sources if it has a name
        if getattr(sourceClass, 'name', None):
            AvailableTileSources[sourceClass.name] = sourceClass
    except ImportError:
        if girder:
            print(TerminalColor.error('Error: Could not import %s' % className))
            logger.exception('Error: Could not import %s' % className)
        else:
            logger.warning('Error: Could not import %s' % className)

__all__ = all
