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

import json
import math
import os
import requests
import struct
import time
from six.moves import range

import girder
from girder import config
from tests import base


# boiler plate to start and stop the server

os.environ['GIRDER_PORT'] = os.environ.get('GIRDER_TEST_PORT', '20200')
config.loadConfig()  # Must reload config to pickup correct port

JPEGHeader = '\xff\xd8\xff'
PNGHeader = '\x89PNG'


def setUpModule():
    base.enabledPlugins.append('large_image')
    base.startServer(False)


def tearDownModule():
    base.stopServer()


class LargeImageTilesTest(base.TestCase):
    def setUp(self):
        base.TestCase.setUp(self)
        admin = {
            'email': 'admin@email.com',
            'login': 'adminlogin',
            'firstName': 'Admin',
            'lastName': 'Last',
            'password': 'adminpassword',
            'admin': True
        }
        self.admin = self.model('user').createUser(**admin)
        folders = self.model('folder').childFolders(
            self.admin, 'user', user=self.admin)
        for folder in folders:
            if folder['name'] == 'Public':
                self.publicFolder = folder
        # Authorize our user for Girder Worker
        resp = self.request(
            '/system/setting', method='PUT', user=self.admin, params={
                'list': json.dumps([{
                    'key': 'worker.broker',
                    'value': 'mongodb://127.0.0.1/girder_worker'
                    }, {
                    'key': 'worker.backend',
                    'value': 'mongodb://127.0.0.1/girder_worker'
                    }])})
        self.assertStatusOk(resp)

    def _uploadFile(self, path):
        """
        Upload the specified path to the admin user's public folder and return
        the resulting item.

        :param path: path to upload.
        :returns: file: the created file.
        """
        name = os.path.basename(path)
        with open(path, 'rb') as file:
            data = file.read()
        resp = self.request(
            path='/file', method='POST', user=self.admin, params={
                'parentType': 'folder',
                'parentId': self.publicFolder['_id'],
                'name': name,
                'size': len(data)
            })
        self.assertStatusOk(resp)
        uploadId = resp.json['_id']

        fields = [('offset', 0), ('uploadId', uploadId)]
        files = [('chunk', name, data)]
        resp = self.multipartRequest(
            path='/file/chunk', fields=fields, files=files, user=self.admin)
        self.assertStatusOk(resp)
        self.assertIn('itemId', resp.json)
        return resp.json

    def _createTestTiles(self, itemId, params={}, info=None, error=None):
        """
        Discard any existing tile set on an item, then create a test tile set
        with some optional parameters.

        :param itemId: the item on which the tiles are created.
        :param params: optional parameters to use for the tiles.
        :param info: if present, the tile information must match all values in
                     this dictionary.
        :param error: if present, expect to get an error from the tile info
                      query and ensure that this string is in the error
                      message.
        :returns: the tile information dictionary.
        """
        # We don't actually use the itemId to fetch test tiles
        try:
            resp = self.request(path='/item/test/tiles', user=self.admin,
                                params=params)
            if error:
                self.assertStatus(resp, 400)
                self.assertIn(error, resp.json['message'])
                return None
        except AssertionError as exc:
            if error:
                self.assertIn(error, exc.args[0])
                return
            else:
                raise
        self.assertStatusOk(resp)
        infoDict = resp.json
        if info:
            for key in info:
                self.assertEqual(infoDict[key], info[key])
        return infoDict

    def _testTilesZXY(self, itemId, metadata, tileParams={},
                      imgHeader=JPEGHeader):
        """
        Test that the tile server is serving images.

        :param itemId: the item ID to get tiles from.
        :param metadata: tile information used to determine the expected
                         valid queries.  If 'sparse' is added to it, tiles
                         are allowed to not exist above that level.
        :param tileParams: optional parameters to send to the tile query.
        :param imgHeader: if something other than a JPEG is expected, this is
                          the first few bytes of the expected image.
        """
        # We should get images for all valid levels, but only within the
        # expected range of tiles.
        for z in range(metadata.get('minLevel', 0), metadata['levels']):
            maxX = math.ceil(float(metadata['sizeX']) * 2 ** (
                z - metadata['levels'] + 1) / metadata['tileWidth']) - 1
            maxY = math.ceil(float(metadata['sizeY']) * 2 ** (
                z - metadata['levels'] + 1) / metadata['tileHeight']) - 1
            # Check the four corners on each level
            for (x, y) in ((0, 0), (maxX, 0), (0, maxY), (maxX, maxY)):
                resp = self.request(path='/item/%s/tiles/zxy/%d/%d/%d' % (
                    itemId, z, x, y), user=self.admin, params=tileParams,
                    isJson=False)
                if (resp.output_status[:3] != '200' and
                        metadata.get('sparse') and z > metadata['sparse']):
                    self.assertStatus(resp, 404)
                    continue
                self.assertStatusOk(resp)
                image = self.getBody(resp, text=False)
                self.assertEqual(image[:len(imgHeader)], imgHeader)
            # Check out of range each level
            for (x, y) in ((-1, 0), (maxX + 1, 0), (0, -1), (0, maxY + 1)):
                resp = self.request(path='/item/%s/tiles/zxy/%d/%d/%d' % (
                    itemId, z, x, y), user=self.admin, params=tileParams)
                if x < 0 or y < 0:
                    self.assertStatus(resp, 400)
                    self.assertTrue('must be positive integers' in
                                    resp.json['message'])
                else:
                    self.assertStatus(resp, 404)
                    self.assertTrue('does not exist' in resp.json['message'] or
                                    'outside layer' in resp.json['message'])
        # Check negative z level
        resp = self.request(path='/item/%s/tiles/zxy/-1/0/0' % itemId,
                            user=self.admin, params=tileParams)
        self.assertStatus(resp, 400)
        self.assertIn('must be positive integers', resp.json['message'])
        # Check non-integer z level
        resp = self.request(path='/item/%s/tiles/zxy/abc/0/0' % itemId,
                            user=self.admin, params=tileParams)
        self.assertStatus(resp, 400)
        self.assertIn('must be integers', resp.json['message'])
        # If we set the minLevel, test one lower than it
        if 'minLevel' in metadata:
            resp = self.request(path='/item/%s/tiles/zxy/%d/0/0' % (
                itemId, metadata['minLevel'] - 1), user=self.admin,
                params=tileParams)
            self.assertStatus(resp, 404)
            self.assertIn('layer does not exist', resp.json['message'])
        # Check too large z level
        resp = self.request(path='/item/%s/tiles/zxy/%d/0/0' % (
            itemId, metadata['levels']), user=self.admin, params=tileParams)
        self.assertStatus(resp, 404)
        self.assertIn('layer does not exist', resp.json['message'])

    def _postTileViaHttp(self, itemId, fileId):
        """
        When we know we need to process a job, we have to use an actual http
        request rather than the normal simulated request to cherrypy.  This is
        required because cherrypy needs to know how it was reached so that
        girder_worker can reach it when done.

        :param itemId: the id of the item with the file to process.
        :param fileId: the id of the file that should be processed.
        :returns: metadata from the tile if the conversion was successful,
                  False if it converted but didn't result in useable tiles, and
                  None if it failed.
        """
        headers = [('Accept', 'application/json')]
        self._buildHeaders(headers, None, self.admin, None, None, None)
        headers = {header[0]: header[1] for header in headers}
        req = requests.post('http://127.0.0.1:%d/api/v1/item/%s/tiles' % (
            int(os.environ['GIRDER_PORT']), itemId), headers=headers,
            data={'fileId': fileId})
        self.assertEqual(req.status_code, 200)
        # If we ask to create the item again right away, we should be told that
        # either there is already a job running or the item has already been
        # added
        req = requests.post('http://127.0.0.1:%d/api/v1/item/%s/tiles' % (
            int(os.environ['GIRDER_PORT']), itemId), headers=headers,
            data={'fileId': fileId})
        self.assertEqual(req.status_code, 400)
        self.assertTrue('Item already has' in req.json()['message'] or
                        'Item is scheduled' in req.json()['message'])

        starttime = time.time()
        resp = None
        while time.time() - starttime < 30:
            try:
                resp = self.request(path='/item/%s/tiles' % itemId,
                                    user=self.admin)
                self.assertStatusOk(resp)
                break
            except AssertionError as exc:
                if 'File must have at least 1 level' in exc.args[0]:
                    return False
                self.assertIn('is still pending creation', exc.args[0])
            item = self.model('item').load(itemId, user=self.admin)
            job = self.model('job', 'jobs').load(item['largeImage']['jobId'],
                                                 user=self.admin)
            if job['status'] == girder.plugins.jobs.constants.JobStatus.ERROR:
                return None
            time.sleep(0.1)
        self.assertStatusOk(resp)
        return resp.json

    def testTilesFromPTIF(self):
        file = self._uploadFile(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_image.ptif'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        # We shouldn't have tile information yet
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatus(resp, 400)
        self.assertIn('No large image file', resp.json['message'])
        resp = self.request(path='/item/%s/tiles/zxy/0/0/0' % itemId,
                            user=self.admin)
        self.assertStatus(resp, 404)
        self.assertIn('No large image file', resp.json['message'])
        # Asking to delete the tile information succeeds but does nothing
        resp = self.request(path='/item/%s/tiles' % itemId, method='DELETE',
                            user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['deleted'], False)
        # Ask to make this a tile-based item with an invalid file ID
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin, params={'fileId': itemId})
        self.assertStatus(resp, 400)
        self.assertIn('No such file', resp.json['message'])

        # Ask to make this a tile-based item properly
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin, params={'fileId': fileId})
        self.assertStatusOk(resp)
        # Now the tile request should tell us about the file.  These are
        # specific to our test file
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatusOk(resp)
        tileMetadata = resp.json
        self.assertEqual(tileMetadata['tileWidth'], 256)
        self.assertEqual(tileMetadata['tileHeight'], 256)
        self.assertEqual(tileMetadata['sizeX'], 58368)
        self.assertEqual(tileMetadata['sizeY'], 12288)
        self.assertEqual(tileMetadata['levels'], 9)
        tileMetadata['sparse'] = 5
        self._testTilesZXY(itemId, tileMetadata)

        # Ask to make this a tile-based item again
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin, params={'fileId': fileId})
        self.assertStatus(resp, 400)
        self.assertIn('Item already has', resp.json['message'])

        # We should be able to delete the large image information
        resp = self.request(path='/item/%s/tiles' % itemId, method='DELETE',
                            user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['deleted'], True)

        # We should no longer have tile informaton
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatus(resp, 400)
        self.assertIn('No large image file', resp.json['message'])

        # We should be able to re-add it (we are also testing that fileId is
        # optional if there is only one file).
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin)
        self.assertStatusOk(resp)
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatusOk(resp)

    def testTilesFromTest(self):
        file = self._uploadFile(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_image.ptif'))
        items = [{'itemId': str(file['itemId']), 'fileId': str(file['_id'])}]
        # Create a second item
        resp = self.request(path='/item', method='POST', user=self.admin,
                            params={'folderId': self.publicFolder['_id'],
                                    'name': 'test'})
        self.assertStatusOk(resp)
        itemId = str(resp.json['_id'])
        items.append({'itemId': itemId})
        # Check that we can't create a tile set with another item's file
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin,
                            params={'fileId': items[0]['fileId']})
        self.assertStatus(resp, 400)
        self.assertIn('The provided file must be in the provided item',
                      resp.json['message'])
        # Now create a test tile with the default options
        params = {'encoding': 'JPEG'}
        meta = self._createTestTiles(itemId, params, {
            'tileWidth': 256, 'tileHeight': 256,
            'sizeX': 256 * 2 ** 9, 'sizeY': 256 * 2 ** 9, 'levels': 10
        })
        self._testTilesZXY('test', meta, params)
        # Test most of our parameters in a single special case
        params = {
            'minLevel': 2,
            'maxLevel': 5,
            'tileWidth': 160,
            'tileHeight': 120,
            'sizeX': 5000,
            'sizeY': 3000,
            'encoding': 'JPEG'
        }
        meta = self._createTestTiles(itemId, params, {
            'tileWidth': 160, 'tileHeight': 120,
            'sizeX': 5000, 'sizeY': 3000, 'levels': 6
        })
        meta['minLevel'] = 2
        self._testTilesZXY('test', meta, params)
        # Test the fractal tiles with PNG
        params = {'fractal': 'true'}
        meta = self._createTestTiles(itemId, params, {
            'tileWidth': 256, 'tileHeight': 256,
            'sizeX': 256 * 2 ** 9, 'sizeY': 256 * 2 ** 9, 'levels': 10
        })
        self._testTilesZXY('test', meta, params, PNGHeader)
        # Test that the fractal isn't the same as the non-fractal
        resp = self.request(path='/item/test/tiles/zxy/0/0/0', user=self.admin,
                            params=params, isJson=False)
        image = self.getBody(resp, text=False)
        resp = self.request(path='/item/test/tiles/zxy/0/0/0', user=self.admin,
                            isJson=False)
        self.assertNotEqual(self.getBody(resp, text=False), image)
        # Test each property with an invalid value
        badParams = {
            'minLevel': 'a',
            'maxLevel': False,
            'tileWidth': (),
            'tileHeight': [],
            'sizeX': {},
            'sizeY': 1.3,
            'encoding': 2,
        }
        for key in badParams:
            err = ('parameter is an incorrect' if key is not 'encoding' else
                   'Invalid encoding')
            self._createTestTiles(itemId, {key: badParams[key]}, error=err)

    def testTilesFromPNG(self):
        file = self._uploadFile(os.path.join(
            os.path.dirname(__file__), 'test_files', 'yb10kx5k.png'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        tileMetadata = self._postTileViaHttp(itemId, fileId)
        self.assertEqual(tileMetadata['tileWidth'], 256)
        self.assertEqual(tileMetadata['tileHeight'], 256)
        self.assertEqual(tileMetadata['sizeX'], 10000)
        self.assertEqual(tileMetadata['sizeY'], 5000)
        self.assertEqual(tileMetadata['levels'], 7)
        self._testTilesZXY(itemId, tileMetadata)
        # Ask to make this a tile-based item with an missing file ID (there are
        # now two files, so this will now fail).
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin)
        self.assertStatus(resp, 400)
        self.assertIn('Missing "fileId"', resp.json['message'])
        # We should be able to delete the tiles
        resp = self.request(path='/item/%s/tiles' % itemId, method='DELETE',
                            user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['deleted'], True)
        # We should no longer have tile informaton
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatus(resp, 400)
        self.assertIn('No large image file', resp.json['message'])
        # This should work with a PNG with transparency, too.
        file = self._uploadFile(os.path.join(
            os.path.dirname(__file__), 'test_files', 'yb10kx5ktrans.png'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        tileMetadata = self._postTileViaHttp(itemId, fileId)
        self.assertEqual(tileMetadata['tileWidth'], 256)
        self.assertEqual(tileMetadata['tileHeight'], 256)
        self.assertEqual(tileMetadata['sizeX'], 10000)
        self.assertEqual(tileMetadata['sizeY'], 5000)
        self.assertEqual(tileMetadata['levels'], 7)
        self._testTilesZXY(itemId, tileMetadata)
        # We should be able to delete the tiles
        resp = self.request(path='/item/%s/tiles' % itemId, method='DELETE',
                            user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['deleted'], True)
        # We should no longer have tile informaton
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatus(resp, 400)
        self.assertIn('No large image file', resp.json['message'])

    def testTilesFromBadFiles(self):
        # Uploading a monochrome file should result in no useful tiles.
        file = self._uploadFile(os.path.join(
            os.path.dirname(__file__), 'test_files', 'small.jpg'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        tileMetadata = self._postTileViaHttp(itemId, fileId)
        self.assertEqual(tileMetadata, False)
        # We should be able to delete the conversion
        resp = self.request(path='/item/%s/tiles' % itemId, method='DELETE',
                            user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['deleted'], True)
        # Uploading a non-image file should run a job, too.
        file = self._uploadFile(os.path.join(
            os.path.dirname(__file__), 'test_files', 'notanimage.txt'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        tileMetadata = self._postTileViaHttp(itemId, fileId)
        self.assertEqual(tileMetadata, None)
        resp = self.request(path='/item/%s/tiles' % itemId, method='DELETE',
                            user=self.admin)
        self.assertStatusOk(resp)
        self.assertEqual(resp.json['deleted'], True)

    def testTilesFromSVS(self):
        file = self._uploadFile(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_svs_image.TCGA-DU-6399-'
            '01A-01-TS1.e8eb65de-d63e-42db-af6f-14fefbbdf7bd.svs'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        # Ask to make this a tile-based item
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin, params={'fileId': fileId})
        self.assertStatusOk(resp)
        # Now the tile request should tell us about the file.  These are
        # specific to our test file
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatusOk(resp)
        tileMetadata = resp.json
        self.assertEqual(tileMetadata['tileWidth'], 240)
        self.assertEqual(tileMetadata['tileHeight'], 240)
        self.assertEqual(tileMetadata['sizeX'], 31872)
        self.assertEqual(tileMetadata['sizeY'], 13835)
        self.assertEqual(tileMetadata['levels'], 9)
        self._testTilesZXY(itemId, tileMetadata)

        # Ask to make this a tile-based item again
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin, params={'fileId': fileId})
        self.assertStatus(resp, 400)
        self.assertIn('Item already has', resp.json['message'])

        # Ask for PNGs
        params = {'encoding': 'PNG'}
        self._testTilesZXY(itemId, tileMetadata, params, PNGHeader)

        # Check that invalid encodings are rejected
        try:
            resp = self.request(path='/item/%s/tiles' % itemId,
                                user=self.admin,
                                params={'encoding': 'invalid'})
            self.assertTrue(False)
        except AssertionError as exc:
            self.assertIn('Invalid encoding', exc.args[0])

        # Check that JPEG options are honored.
        resp = self.request(path='/item/%s/tiles/zxy/0/0/0' % itemId,
                            user=self.admin, isJson=False)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        defaultLength = len(image)

        resp = self.request(path='/item/%s/tiles/zxy/0/0/0' % itemId,
                            user=self.admin, isJson=False,
                            params={'jpegQuality': 10})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        self.assertTrue(len(image) < defaultLength)

        resp = self.request(path='/item/%s/tiles/zxy/0/0/0' % itemId,
                            user=self.admin, isJson=False,
                            params={'jpegSubsampling': 2})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        self.assertTrue(len(image) < defaultLength)

    def testDummyTileSource(self):
        # We can't actually load the dummy source via the endpoints if we have
        # all of the requirements installed, so just check that it exists and
        # will return appropriate values.
        from girder.plugins.large_image.tilesource.dummy import DummyTileSource
        dummy = DummyTileSource()
        self.assertEqual(dummy.getTile(0, 0, 0), '')
        tileMetadata = dummy.getMetadata()
        self.assertEqual(tileMetadata['tileWidth'], 0)
        self.assertEqual(tileMetadata['tileHeight'], 0)
        self.assertEqual(tileMetadata['sizeX'], 0)
        self.assertEqual(tileMetadata['sizeY'], 0)
        self.assertEqual(tileMetadata['levels'], 0)

    def testThumbnails(self):
        file = self._uploadFile(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_image.ptif'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        # We shouldn't be able to get a thumbnail yet
        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin)
        self.assertStatus(resp, 400)
        self.assertIn('No large image file', resp.json['message'])
        # Ask to make this a tile-based item
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin, params={'fileId': fileId})
        self.assertStatusOk(resp)
        # Get metadata to use in our thumbnail tests
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatusOk(resp)
        tileMetadata = resp.json
        # Now we should be able to get a thumbnail
        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin, isJson=False)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        defaultLength = len(image)

        # Test that JPEG options are honored
        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin, isJson=False,
                            params={'jpegQuality': 10})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        self.assertTrue(len(image) < defaultLength)

        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin, isJson=False,
                            params={'jpegSubsampling': 2})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        self.assertTrue(len(image) < defaultLength)

        # Test width and height using PNGs
        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin, isJson=False,
                            params={'encoding': 'PNG'})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(PNGHeader)], PNGHeader)
        (width, height) = struct.unpack('!LL', image[16:24])
        self.assertEqual(max(width, height), 256)
        # We know that we are using an example where the width is greater than
        # the height
        origWidth = int(tileMetadata['sizeX'] *
                        2 ** -(tileMetadata['levels'] - 1))
        origHeight = int(tileMetadata['sizeY'] *
                         2 ** -(tileMetadata['levels'] - 1))
        self.assertEqual(height, int(width * origHeight / origWidth))
        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin, isJson=False,
                            params={'encoding': 'PNG', 'width': 200})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(PNGHeader)], PNGHeader)
        (width, height) = struct.unpack('!LL', image[16:24])
        self.assertEqual(width, 200)
        self.assertEqual(height, int(width * origHeight / origWidth))
        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin, isJson=False,
                            params={'encoding': 'PNG', 'height': 200})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(PNGHeader)], PNGHeader)
        (width, height) = struct.unpack('!LL', image[16:24])
        self.assertEqual(height, 200)
        self.assertEqual(width, int(height * origWidth / origHeight))
        resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                            user=self.admin, isJson=False,
                            params={'encoding': 'PNG',
                                    'width': 180, 'height': 180})
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(PNGHeader)], PNGHeader)
        (width, height) = struct.unpack('!LL', image[16:24])
        self.assertEqual(width, 180)
        self.assertEqual(height, int(width * origHeight / origWidth))

        # Test bad parameters
        badParams = [
            ({'encoding': 'invalid'}, 400, 'Invalid encoding'),
            ({'width': 'invalid'}, 400, 'incorrect type'),
            ({'width': 0}, 400, 'Invalid width or height'),
            ({'width': -5}, 400, 'Invalid width or height'),
            ({'height': 'invalid'}, 400, 'incorrect type'),
            ({'height': 0}, 400, 'Invalid width or height'),
            ({'height': -5}, 400, 'Invalid width or height'),
            ({'jpegQuality': 'invalid'}, 400, 'incorrect type'),
            ({'jpegSubsampling': 'invalid'}, 400, 'incorrect type'),
        ]
        for entry in badParams:
            resp = self.request(path='/item/%s/tiles/thumbnail' % itemId,
                                user=self.admin,
                                params=entry[0])
            self.assertStatus(resp, entry[1])
            self.assertIn(entry[2], resp.json['message'])

    def testRegions(self):
        file = self._uploadFile(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_image.ptif'))
        itemId = str(file['itemId'])
        # We shouldn't be able to get a region yet
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin)
        self.assertStatus(resp, 400)
        self.assertIn('No large image file', resp.json['message'])
        # Ask to make this a tile-based item
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin)
        self.assertStatusOk(resp)
        # Get metadata to use in our tests
        resp = self.request(path='/item/%s/tiles' % itemId, user=self.admin)
        self.assertStatusOk(resp)
        tileMetadata = resp.json

        # Test bad parameters
        badParams = [
            ({'encoding': 'invalid', 'width': 10}, 400, 'Invalid encoding'),
            ({'width': 'invalid'}, 400, 'incorrect type'),
            ({'width': -5}, 400, 'Invalid width or height'),
            ({'height': 'invalid'}, 400, 'incorrect type'),
            ({'height': -5}, 400, 'Invalid width or height'),
            ({'jpegQuality': 'invalid', 'width': 10}, 400, 'incorrect type'),
            ({'jpegSubsampling': 'invalid', 'width': 10}, 400,
             'incorrect type'),
            ({'left': 'invalid'}, 400, 'incorrect type'),
            ({'right': 'invalid'}, 400, 'incorrect type'),
            ({'top': 'invalid'}, 400, 'incorrect type'),
            ({'bottom': 'invalid'}, 400, 'incorrect type'),
            ({'regionWidth': 'invalid'}, 400, 'incorrect type'),
            ({'regionHeight': 'invalid'}, 400, 'incorrect type'),
            ({'units': 'invalid'}, 400, 'Invalid units'),
        ]
        for entry in badParams:
            resp = self.request(path='/item/%s/tiles/region' % itemId,
                                user=self.admin,
                                params=entry[0])
            self.assertStatus(resp, entry[1])
            self.assertIn(entry[2], resp.json['message'])

        # Get a small region for testing.  Our test file is sparse, so
        # initially get a region where there is full information.
        params = {'regionWidth': 1000, 'regionHeight': 1000,
                  'left': 48000, 'top': 3000}
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = origImage = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        defaultLength = len(image)

        # Test that JPEG options are honored
        params['jpegQuality'] = 10
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        self.assertTrue(len(image) < defaultLength)
        del params['jpegQuality']

        params['jpegSubsampling'] = 2
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
        self.assertTrue(len(image) < defaultLength)
        del params['jpegSubsampling']

        # Test using negative offsets
        params['left'] -= tileMetadata['sizeX']
        params['top'] -= tileMetadata['sizeY']
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image, origImage)
        # We should get the same image using right and bottom
        params = {
            'left': params['left'], 'top': params['top'],
            'right': params['left'] + 1000, 'bottom': params['top'] + 1000}
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image, origImage)
        params = {
            'regionWidth': 1000, 'regionHeight': 1000,
            'right': params['right'], 'bottom': params['bottom']}
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image, origImage)

        # Fractions should get us the same results
        params = {
            'regionWidth': 1000.0 / tileMetadata['sizeX'],
            'regionHeight': 1000.0 / tileMetadata['sizeY'],
            'left': 48000.0 / tileMetadata['sizeX'],
            'top': 3000.0 / tileMetadata['sizeY'],
            'units': 'fraction'}
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image, origImage)

        # 0-sized results are allowed
        params = {'regionWidth': 1000, 'regionHeight': 0,
                  'left': 48000, 'top': 3000, 'width': 1000, 'height': 1000}
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(len(image), 0)

        # Test scaling (and a sparse region from our file)
        params = {'regionWidth': 2000, 'regionHeight': 1500,
                  'width': 500, 'height': 500, 'encoding': 'PNG'}
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(PNGHeader)], PNGHeader)
        (width, height) = struct.unpack('!LL', image[16:24])
        self.assertEqual(width, 500)
        self.assertEqual(height, 375)

        # test svs image
        file = self._uploadFile(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_svs_image.TCGA-DU-6399-'
            '01A-01-TS1.e8eb65de-d63e-42db-af6f-14fefbbdf7bd.svs'))
        itemId = str(file['itemId'])
        # Ask to make this a tile-based item
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin)
        self.assertStatusOk(resp)
        params = {'regionWidth': 2000, 'regionHeight': 1500,
                  'width': 1000, 'height': 1000, 'encoding': 'PNG'}
        resp = self.request(path='/item/%s/tiles/region' % itemId,
                            user=self.admin, isJson=False, params=params)
        self.assertStatusOk(resp)
        image = self.getBody(resp, text=False)
        self.assertEqual(image[:len(PNGHeader)], PNGHeader)
        (width, height) = struct.unpack('!LL', image[16:24])
        self.assertEqual(width, 1000)
        self.assertEqual(height, 750)

    def testSettings(self):
        from girder.plugins.large_image import constants
        from girder.models.model_base import ValidationException

        for key in (constants.PluginSettings.LARGE_IMAGE_SHOW_THUMBNAILS,
                    constants.PluginSettings.LARGE_IMAGE_SHOW_VIEWER):
            self.model('setting').set(key, 'false')
            self.assertFalse(self.model('setting').get(key))
            self.model('setting').set(key, 'true')
            self.assertTrue(self.model('setting').get(key))
            try:
                self.model('setting').set(key, 'not valid')
                self.assertTrue(False)
            except ValidationException as exc:
                self.assertIn('Invalid setting', exc.args[0])
        self.model('setting').set(
            constants.PluginSettings.LARGE_IMAGE_DEFAULT_VIEWER, 'geojs')
        self.assertEqual(self.model('setting').get(
            constants.PluginSettings.LARGE_IMAGE_DEFAULT_VIEWER), 'geojs')
        # Test the system/setting/large_image end point
        resp = self.request(path='/system/setting/large_image', user=None)
        self.assertStatusOk(resp)
        settings = resp.json
        # The values were set earlier
        self.assertEqual(settings[
            constants.PluginSettings.LARGE_IMAGE_DEFAULT_VIEWER], 'geojs')
        self.assertEqual(settings[
            constants.PluginSettings.LARGE_IMAGE_SHOW_VIEWER], True)
        self.assertEqual(settings[
            constants.PluginSettings.LARGE_IMAGE_SHOW_THUMBNAILS], True)

    def testGetTileSource(self):
        from girder.plugins.large_image.tilesource import getTileSource

        # Upload a PTIF and make it a large_image
        file = self._uploadFile(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_image.ptif'))
        itemId = str(file['itemId'])
        fileId = str(file['_id'])
        resp = self.request(path='/item/%s/tiles' % itemId, method='POST',
                            user=self.admin, params={'fileId': fileId})
        self.assertStatusOk(resp)
        # We should have access via getTileSource
        source = getTileSource('girder_item://' + itemId, user=self.admin)
        image, mime = source.getThumbnail(encoding='PNG', height=200)
        self.assertEqual(image[:len(PNGHeader)], PNGHeader)

        # We can also use a file with getTileSource.  The user is ignored.
        source = getTileSource(os.path.join(
            os.environ['LARGE_IMAGE_DATA'], 'sample_svs_image.TCGA-DU-6399-'
            '01A-01-TS1.e8eb65de-d63e-42db-af6f-14fefbbdf7bd.svs'),
            user=self.admin, encoding='PNG')
        image, mime = source.getThumbnail(encoding='JPEG', width=200)
        self.assertEqual(image[:len(JPEGHeader)], JPEGHeader)
