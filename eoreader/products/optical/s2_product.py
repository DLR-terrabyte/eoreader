# -*- coding: utf-8 -*-
# Copyright 2022, SERTIT-ICube - France, https://sertit.unistra.fr/
# This file is part of eoreader project
#     https://github.com/sertit/eoreader
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Sentinel-2 products """
import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import datetime
from enum import unique
from pathlib import Path
from typing import Union

import geopandas as gpd
import numpy as np
import rasterio
import xarray as xr
from affine import Affine
from cloudpathlib import CloudPath
from lxml import etree
from rasterio import errors, features, transform
from rasterio.crs import CRS
from rasterio.enums import Resampling
from sertit import files, rasters, vectors
from sertit.misc import ListEnum
from shapely.geometry import box

from eoreader import cache, utils
from eoreader.bands import (
    ALL_CLOUDS,
    CIRRUS,
    CLOUDS,
    RAW_CLOUDS,
    SHADOWS,
    BandNames,
    SpectralBand,
)
from eoreader.bands import spectral_bands as spb
from eoreader.bands import to_str
from eoreader.exceptions import InvalidProductError, InvalidTypeError
from eoreader.products import OpticalProduct
from eoreader.products.product import OrbitDirection
from eoreader.stac import CENTER_WV, FWHM, GSD, ID, NAME
from eoreader.utils import DATETIME_FMT, EOREADER_NAME

LOGGER = logging.getLogger(EOREADER_NAME)


@unique
class S2ProductType(ListEnum):
    """Sentinel-2 products types (L1C or L2A)"""

    L1C = "MSIL1C"
    """Level-1C: https://sentinel.esa.int/web/sentinel/user-guides/sentinel-2-msi/product-types/level-1c"""

    L2A = "MSIL2A"
    """Level-2A: https://sentinel.esa.int/web/sentinel/user-guides/sentinel-2-msi/product-types/level-2a"""


@unique
class S2GmlMasks(ListEnum):
    """Sentinel-2 GML masks (processing baseline < 4.0)"""

    FOOTPRINT = "DETFOO"
    CLOUDS = "CLOUDS"
    DEFECT = "DEFECT"
    NODATA = "NODATA"
    SATURATION = "SATURA"
    QUALITY = "TECQUA"

    # L2A (jp2)
    CLDPRB = "CLDPRB"
    SNWPRB = "SNWPRB"


@unique
class S2Jp2Masks(ListEnum):
    """Sentinel-2 jp2 masks (processing baseline > 4.0)"""

    # Both L1C and L2A
    FOOTPRINT = "DETFOO"
    CLOUDS = "CLASSI"
    QUALITY = "QUALIT"  # Regroups TECQUA, DEFECT, NODATA, SATURA

    # L2A
    CLDPRB = "CLDPRB"
    SNWPRB = "SNWPRB"


BAND_DIR_NAMES = {
    S2ProductType.L1C: "IMG_DATA",
    S2ProductType.L2A: {
        "01": ["R60m"],
        "02": ["R10m", "R20m", "R60m"],
        "03": ["R10m", "R20m", "R60m"],
        "04": ["R10m", "R20m", "R60m"],
        "05": ["R20m", "R60m"],
        "06": ["R20m", "R60m"],
        "07": ["R20m", "R60m"],
        "08": ["R10m"],
        "8A": ["R20m", "R60m"],
        "09": ["R60m"],
        "11": ["R20m", "R60m"],
        "12": ["R20m", "R60m"],
    },
}


class S2Product(OpticalProduct):
    """
    Class of Sentinel-2 Products

    You can use directly the .zip file
    """

    def __init__(
        self,
        product_path: Union[str, CloudPath, Path],
        archive_path: Union[str, CloudPath, Path] = None,
        output_path: Union[str, CloudPath, Path] = None,
        remove_tmp: bool = False,
        **kwargs,
    ) -> None:

        # Processing baseline < 02.07: images not georeferenced (L2Ap and after)

        # Is this products comes from a processing baseline less than 4.0
        # The processing baseline 4.0 introduces format changes:
        # - masks are given as GeoTIFFs instead of GML files
        # - an offset is added to keep the zero as no-data value
        # See here for more information
        # https://sentinels.copernicus.eu/web/sentinel/-/copernicus-sentinel-2-major-products-upgrade-upcoming
        self._processing_baseline = None
        self.base_no_data_val = 0
        self.no_data_val = {}

        # L2Ap
        self._is_l2ap = False

        # Initialization from the super class
        super().__init__(product_path, archive_path, output_path, remove_tmp, **kwargs)

        try:
            self.read_mtd()
        except InvalidProductError:
            LOGGER.warning(
                f"Corrupted metadata for {self.path}. "
                f"Trying to process this product in degraded mode. "
                f"Every process needing something from the metadata won't be able to be computed (i.e. HILLSHADE)"
            )

    def _pre_init(self, **kwargs) -> None:
        """
        Function used to pre_init the products
        (setting needs_extraction and so on)
        """
        self._has_cloud_cover = True
        self.needs_extraction = False
        self._use_filename = True

        # Post init done by the super class
        super()._pre_init(**kwargs)

    def _post_init(self, **kwargs) -> None:
        """
        Function used to post_init the products
        (setting sensor type, band names and so on)
        """
        self.tile_name = self._get_tile_name()

        # Get processing baseline: N0213 -> 02.13
        pr_baseline = float(self.split_name[3][1:]) / 100
        self._processing_baseline = pr_baseline

        # Post init done by the super class
        super()._post_init(**kwargs)

    def _set_resolution(self) -> float:
        """
        Set product default resolution (in meters)
        """
        # S2: use 10m resolution, even if we have 60m and 20m resolution
        # In the future maybe use one resolution per band ?
        return 10.0

    def _get_tile_name(self) -> str:
        """
        Retrieve tile name

        Returns:
            str: Tile name
        """
        return self.split_name[-2]

    def _set_product_type(self) -> None:
        """Set products type"""
        self.product_type = S2ProductType.from_value(self.split_name[1])

    def _map_bands(self) -> None:
        """
        Map bands
        """
        l2a_bands = {
            spb.CA: SpectralBand(
                eoreader_name=spb.CA,
                **{NAME: "B01", ID: "01", GSD: 60, CENTER_WV: 442, FWHM: 21},
            ),
            spb.BLUE: SpectralBand(
                eoreader_name=spb.BLUE,
                **{NAME: "B02", ID: "02", GSD: 10, CENTER_WV: 492, FWHM: 66},
            ),
            spb.GREEN: SpectralBand(
                eoreader_name=spb.GREEN,
                **{NAME: "B03", ID: "03", GSD: 10, CENTER_WV: 560, FWHM: 36},
            ),
            spb.RED: SpectralBand(
                eoreader_name=spb.RED,
                **{NAME: "B04", ID: "04", GSD: 10, CENTER_WV: 665, FWHM: 31},
            ),
            spb.VRE_1: SpectralBand(
                eoreader_name=spb.VRE_1,
                **{NAME: "B05", ID: "05", GSD: 20, CENTER_WV: 704, FWHM: 15},
            ),
            spb.VRE_2: SpectralBand(
                eoreader_name=spb.VRE_2,
                **{NAME: "B06", ID: "06", GSD: 20, CENTER_WV: 740, FWHM: 15},
            ),
            spb.VRE_3: SpectralBand(
                eoreader_name=spb.VRE_3,
                **{NAME: "B07", ID: "07", GSD: 20, CENTER_WV: 781, FWHM: 20},
            ),
            spb.NIR: SpectralBand(
                eoreader_name=spb.NIR,
                **{NAME: "B8A", ID: "8A", GSD: 10, CENTER_WV: 833, FWHM: 106},
            ),
            spb.NARROW_NIR: SpectralBand(
                eoreader_name=spb.NARROW_NIR,
                **{NAME: "B08", ID: "08", GSD: 20, CENTER_WV: 864, FWHM: 21},
            ),
            spb.WV: SpectralBand(
                eoreader_name=spb.WV,
                **{NAME: "B09", ID: "09", GSD: 60, CENTER_WV: 944, FWHM: 20},
            ),
            spb.SWIR_1: SpectralBand(
                eoreader_name=spb.SWIR_1,
                **{NAME: "B11", ID: "11", GSD: 20, CENTER_WV: 1612, FWHM: 92},
            ),
            spb.SWIR_2: SpectralBand(
                eoreader_name=spb.SWIR_2,
                **{NAME: "B12", ID: "12", GSD: 20, CENTER_WV: 2190, FWHM: 180},
            ),
        }

        if self.product_type == S2ProductType.L2A:
            self.bands.map_bands(l2a_bands)
        elif self.product_type == S2ProductType.L1C:
            self.bands.map_bands(
                {
                    **l2a_bands,
                    spb.SWIR_CIRRUS: SpectralBand(
                        eoreader_name=spb.SWIR_CIRRUS,
                        **{NAME: "B10", ID: "10", GSD: 60, CENTER_WV: 1380, FWHM: 30},
                    ),
                }
            )
        else:
            raise InvalidProductError(f"Invalid Sentinel-2 name: {self.filename}")

    @cache
    def crs(self) -> CRS:
        """
        Get UTM projection of the tile

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.crs()
            CRS.from_epsg(32630)

        Returns:
            rasterio.crs.CRS: CRS object
        """
        if self._processing_baseline < 2.07:
            try:
                root, ns = self.read_mtd()
                crs = CRS.from_string(root.findtext(".//HORIZONTAL_CS_CODE"))
            except InvalidProductError:
                # Manage broken XML
                utm_nb = self.tile_name[1:3]
                utm_letter = self.tile_name[3]
                utm_hemisphere = 6 if utm_letter > "N" else 7
                crs = CRS.from_string(f"epsg:32{utm_hemisphere}{utm_nb}")
        else:
            crs = super().crs()

        return crs

    @cache
    def extent(self) -> gpd.GeoDataFrame:
        """
        Get UTM extent of the tile

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.extent()
                                                        geometry
            0  POLYGON ((309780.000 4390200.000, 309780.000 4...

        Returns:
            gpd.GeoDataFrame: Extent in UTM
        """
        if self._processing_baseline < 2.07:
            tf, width, height, crs = self.default_transform()
            bounds = transform.array_bounds(height, width, tf)
            return gpd.GeoDataFrame(geometry=[box(*bounds)], crs=crs)
        else:
            return super().extent()

    @cache
    def footprint(self) -> gpd.GeoDataFrame:
        """
        Get UTM footprint in UTM of the products (without nodata, *in french == emprise utile*)

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.footprint()
               index                                           geometry
            0      0  POLYGON ((199980.000 4500000.000, 199980.000 4...

        Returns:
            gpd.GeoDataFrame: Footprint as a GeoDataFrame
        """
        def_band = self.bands[self.get_default_band()].id
        if self._processing_baseline < 4.0:
            det_footprint = self._open_mask_lt_4_0(S2GmlMasks.FOOTPRINT, def_band)
            footprint_gs = det_footprint.dissolve().convex_hull
            footprint = gpd.GeoDataFrame(
                geometry=footprint_gs.geometry, crs=footprint_gs.crs
            )
        else:
            det_footprint = self._open_mask_gt_4_0(S2Jp2Masks.FOOTPRINT, def_band)
            footprint = rasters.vectorize(
                det_footprint, values=0, keep_values=False, dissolve=True
            )

            # Keep only the convex hull
            footprint.geometry = footprint.geometry.convex_hull

        return footprint

    def get_datetime(self, as_datetime: bool = False) -> Union[str, datetime]:
        """
        Get the product's acquisition datetime, with format :code:`YYYYMMDDTHHMMSS` <-> :code:`%Y%m%dT%H%M%S`

        .. WARNING::
            Sentinel-2 datetime is the datatake sensing time, not the granule sensing time !
            (the one displayed in the product's name)

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.get_datetime(as_datetime=True)
            datetime.datetime(2020, 8, 24, 11, 6, 31)
            >>> prod.get_datetime(as_datetime=False)
            '20200824T110631'

        Args:
            as_datetime (bool): Return the date as a datetime.datetime. If false, returns a string.

        Returns:
             Union[str, datetime.datetime]: Its acquisition datetime
        """
        if self.datetime is None:
            # Sentinel-2 datetime (in the filename) is the datatake sensing time, not the granule sensing time !
            sensing_time = self.split_name[2]

            # Convert to datetime
            date = datetime.strptime(sensing_time, "%Y%m%dT%H%M%S")
        else:
            date = self.datetime

        if not as_datetime:
            date = date.strftime(DATETIME_FMT)

        return date

    def _get_name_sensor_specific(self) -> str:
        """
        Set product real name from metadata

        Returns:
            str: True name of the product (from metadata)
        """
        try:
            # Get MTD XML file
            root, _ = self.read_datatake_mtd()

            # Open identifier
            name = root.findtext(".//PRODUCT_URI")
            if not name:
                # Manage L2Ap products
                name = root.findtext(".//PRODUCT_URI_2A")
                if not name:
                    raise InvalidProductError("PRODUCT_URI not found in metadata!")

            name = files.get_filename(name)
        except InvalidProductError:
            try:
                tile_info = files.read_json(
                    next(self.path.glob("**/tileInfo.json")), print_file=False
                )
                name = tile_info["productName"]
            except json.JSONDecodeError:
                raise InvalidProductError(
                    f"Corrupted metadata and bad filename for {self.path}! "
                    f"Impossible to process this product."
                )

        return name

    def _get_res_band_folder(self, band_list: list, resolution: float = None) -> dict:
        """
        Return the folder containing the bands of a proper S2 products.
        (IMG_DATA for L1C, IMG_DATA/Rx0m for L2A)

        Args:
            band_list (list): Wanted bands (listed as 01, 02...)
            resolution (float): Band resolution for Sentinel-2 products {R10m, R20m, R60m}.
                                The wanted bands will be chosen in this proper folder.

        Returns:
            dict: Dictionary containing the folder path for each queried band
        """
        if resolution is not None:
            if isinstance(resolution, (list, tuple)):
                resolution = resolution[0]

        # Open the band directory names
        s2_bands_folder = {}

        # Manage L2A
        band_dir = BAND_DIR_NAMES[self.product_type]
        for band in band_list:
            band_id = self.bands[band].id
            if band_id is None:
                raise InvalidProductError(
                    f"Non existing band ({band.name}) for S2-{self.product_type.name} products"
                )

            # If L2A products, we care about the resolution
            if self.product_type == S2ProductType.L2A:
                # If we got a true S2 resolution, open the corresponding band
                if resolution and f"R{int(resolution)}m" in band_dir[band_id]:
                    dir_name = f"R{int(resolution)}m"

                # Else open the first one, it will be resampled when the band will be read
                else:
                    dir_name = band_dir[band_id][0]
            # If L1C, we do not
            else:
                dir_name = band_dir

            if self.is_archived:
                # Open the zip file
                with zipfile.ZipFile(self.path, "r") as zip_ds:
                    # Get the band folder (use dirname is the first of the list is a band)
                    band_path = [
                        os.path.dirname(f.filename)
                        for f in zip_ds.filelist
                        if dir_name in f.filename
                    ][0]

                    # Workaround for a bug involving some bad archives
                    if band_path.startswith("/"):
                        band_path = band_path[1:]
                    s2_bands_folder[band] = band_path
            else:
                # Search for the name of the folder into the S2 products
                s2_bands_folder[band] = next(self.path.glob(f"**/*/{dir_name}"))

        for band in band_list:
            if band not in s2_bands_folder:
                raise InvalidProductError(
                    f"Band folder for band {band.value} not found in {self.path}"
                )

        return s2_bands_folder

    def get_band_paths(
        self, band_list: list, resolution: float = None, **kwargs
    ) -> dict:
        """
        Return the paths of required bands.

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> from eoreader.bands import *
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.get_band_paths([GREEN, RED])
            {
                <SpectralBandNames.GREEN: 'GREEN'>: 'zip+file://S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip!/S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE/GRANULE/L1C_T30TTK_A027018_20200824T111345/IMG_DATA/T30TTK_20200824T110631_B03.jp2',
                <SpectralBandNames.RED: 'RED'>: 'zip+file://S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip!/S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE/GRANULE/L1C_T30TTK_A027018_20200824T111345/IMG_DATA/T30TTK_20200824T110631_B04.jp2'
            }

        Args:
            band_list (list): List of the wanted bands
            resolution (float): Band resolution
            kwargs: Other arguments used to load bands

        Returns:
            dict: Dictionary containing the path of each queried band
        """
        band_folders = self._get_res_band_folder(band_list, resolution)
        band_paths = {}
        for band in band_list:
            # Get clean band path
            clean_band = self._get_clean_band_path(
                band, resolution=resolution, **kwargs
            )
            if clean_band.is_file():
                band_paths[band] = clean_band
            else:
                band_id = self.bands[band].id
                try:
                    if self.is_archived:
                        band_paths[band] = files.get_archived_rio_path(
                            self.path,
                            f".*{band_folders[band]}.*_B{band_id}.*.jp2",
                        )
                    else:
                        band_paths[band] = files.get_file_in_dir(
                            band_folders[band],
                            f"_B{band_id}",
                            extension="jp2",
                        )
                except (FileNotFoundError, IndexError) as ex:
                    raise InvalidProductError(
                        f"Non existing {band} ({band_id}) band for {self.path}"
                    ) from ex

        return band_paths

    def _read_band(
        self,
        path: Union[str, CloudPath, Path],
        band: BandNames = None,
        resolution: Union[tuple, list, float] = None,
        size: Union[list, tuple] = None,
        **kwargs,
    ) -> xr.DataArray:
        """
        Read band from disk.

        .. WARNING::
            Invalid pixels are not managed here

        Args:
            path (Union[CloudPath, Path]): Band path
            band (BandNames): Band to read
            resolution (Union[tuple, list, float]): Resolution of the wanted band, in dataset resolution unit (X, Y)
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
            kwargs: Other arguments used to load bands
        Returns:
            xr.DataArray: Band xarray

        """
        geocoded_path = path

        # For L2Ap
        try:
            if self._processing_baseline < 2.07 and str(path).endswith(".jp2"):
                # Get and write geocode data if not already existing
                with rasterio.open(str(path), "r") as ds:
                    if not ds.crs:
                        # Download path just in case
                        on_disk_path = self._get_band_folder(writable=True) / path.name
                        if not on_disk_path.is_file():
                            if isinstance(path, CloudPath):
                                geocoded_path = path.download_to(
                                    self._get_band_folder(writable=True)
                                )
                            else:
                                geocoded_path = files.copy(
                                    path, self._get_band_folder(writable=True)
                                )
                        else:
                            geocoded_path = on_disk_path

                        # Get and write geocode data if not already existing
                        with rasterio.open(str(geocoded_path), "r+") as ds:
                            tf, _, _, crs = self._l2ap_geocode_data(path)
                            ds.crs = crs
                            ds.transform = tf
        except errors.RasterioIOError as ex:
            if str(path).endswith("jp2") or str(path).endswith("tif"):
                raise InvalidProductError(f"Corrupted file: {path}") from ex
            else:
                raise

        # Read band
        return utils.read(
            geocoded_path,
            resolution=resolution,
            size=size,
            resampling=Resampling.bilinear,
            **kwargs,
        )

    def _to_reflectance(
        self,
        band_arr: xr.DataArray,
        path: Union[Path, CloudPath],
        band: BandNames,
        **kwargs,
    ) -> xr.DataArray:
        """
        Converts band to reflectance

        Args:
            band_arr (xr.DataArray): Band array to convert
            path (Union[CloudPath, Path]): Band path
            band (BandNames): Band to read
            **kwargs: Other keywords

        Returns:
            xr.DataArray: Band in reflectance
        """
        if str(path).endswith(".jp2"):
            try:
                # Get MTD XML file
                root, _ = self.read_datatake_mtd()

                # Get quantification value
                quantif_prefix = (
                    "BOA_" if self.product_type == S2ProductType.L2A else ""
                )
                try:
                    quantif_value = float(
                        root.findtext(f".//{quantif_prefix}QUANTIFICATION_VALUE")
                    )
                except TypeError:
                    raise InvalidProductError(
                        f"{quantif_prefix}QUANTIFICATION_VALUE not found in datatake metadata!"
                    )

                # Get offset
                offset_prefix = (
                    "BOA_" if self.product_type == S2ProductType.L2A else "RADIO_"
                )
                if self._processing_baseline < 4.0:
                    offset = 0.0
                else:
                    try:
                        if band == spb.NIR:
                            band_id = 8
                        else:
                            band_id = int(self.bands[band].id)
                        offset = float(
                            root.findtext(
                                f".//{offset_prefix}ADD_OFFSET[@band_id = '{band_id}']"
                            )
                        )
                    except TypeError:
                        raise InvalidProductError(
                            f"{offset_prefix}ADD_OFFSET not found in datatake metadata!"
                        )
            except InvalidProductError:
                # If not datatake file
                if self._processing_baseline < 4.0:
                    offset = 0.0
                else:
                    offset = -1000.0
                quantif_value = 10000.0

            # Compute the correct radiometry of the band
            band_arr = (band_arr + offset) / quantif_value

            self.no_data_val[band] = (self.base_no_data_val + offset) / quantif_value

        return band_arr.astype(np.float32)

    def _open_mask_lt_4_0(
        self, mask_id: Union[str, S2GmlMasks], band: Union[BandNames, str] = None
    ) -> gpd.GeoDataFrame:
        """
        Open S2 mask (GML files stored in QI_DATA) as :code:`gpd.GeoDataFrame`.

        Masks than can be called that way are:

        - :code:`TECQUA`: Technical quality mask
        - :code:`SATURA`: Saturated Pixels
        - :code:`NODATA`: Pixel nodata (inside the detectors)
        - :code:`DETFOO`: Detectors footprint -> used to process nodata outside the detectors
        - :code:`DEFECT`: Defective pixels
        - :code:`CLOUDS`, **only with :code:`00` as a band !**

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> from eoreader.bands import *
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod.open_mask("NODATA", GREEN)
            Empty GeoDataFrame
            Columns: [geometry]
            Index: []
            >>> prod.open_mask("SATURA", GREEN)
            Empty GeoDataFrame
            Columns: [geometry]
            Index: []
            >>> prod.open_mask("DETFOO", GREEN)
                                    gml_id  ...                                           geometry
            0  detector_footprint-B03-02-0  ...  POLYGON Z ((199980.000 4500000.000 0.000, 1999...
            1  detector_footprint-B03-03-1  ...  POLYGON Z ((222570.000 4500000.000 0.000, 2225...
            2  detector_footprint-B03-05-2  ...  POLYGON Z ((273050.000 4500000.000 0.000, 2730...
            3  detector_footprint-B03-07-3  ...  POLYGON Z ((309770.000 4453710.000 0.000, 3097...
            4  detector_footprint-B03-04-4  ...  POLYGON Z ((248080.000 4500000.000 0.000, 2480...
            5  detector_footprint-B03-06-5  ...  POLYGON Z ((297980.000 4500000.000 0.000, 2979...
            [6 rows x 3 columns]

        Args:
            mask_id (Union[str, S2GmlMasks]): Mask name, such as DEFECT, NODATA, SATURA...
            band (Union[BandNames, str]): Band number as an SpectralBandNames or str (for clouds: 00)

        Returns:
            gpd.GeoDataFrame: Mask as a vector
        """
        # Check inputs
        mask_id = S2GmlMasks.from_value(mask_id)
        if mask_id == S2GmlMasks.CLOUDS:
            band = "00"

        # Get QI_DATA path
        if isinstance(band, BandNames):
            band_name = self.bands[band].id
        else:
            band_name = band

        tmp_dir = tempfile.TemporaryDirectory()
        try:
            if self.is_archived:
                # Open the zip file
                # WE DON'T KNOW WHY BUT DO NOT USE files.read_archived_vector HERE !!!
                with zipfile.ZipFile(self.path, "r") as zip_ds:
                    filenames = [f.filename for f in zip_ds.filelist]
                    regex = re.compile(
                        f".*GRANULE.*QI_DATA.*MSK_{mask_id.value}_B{band_name}.gml"
                    )
                    mask_path = zip_ds.extract(
                        list(filter(regex.match, filenames))[0], tmp_dir.name
                    )
            else:
                # Get mask path
                mask_path = files.get_file_in_dir(
                    self.path,
                    f"**/*GRANULE/*/QI_DATA/MSK_{mask_id.value}_B{band_name}.gml",
                    exact_name=True,
                )

            # Read vector
            mask = vectors.read(mask_path, crs=self.crs())

        except Exception as ex:
            raise InvalidProductError(ex) from ex

        finally:
            tmp_dir.cleanup()

        return mask

    def _open_mask_gt_4_0(
        self,
        mask_id: Union[str, S2Jp2Masks],
        band: Union[BandNames, str] = None,
        resolution: float = None,
        size: Union[list, tuple] = None,
        **kwargs,
    ) -> xr.DataArray:
        """
        Open S2 mask (jp2 files stored in QI_DATA) as raster.

        Masks than can be called that way are:

        - :code:`DETFOO`: Detectors footprint -> used to process nodata outside the detectors
        - :code:`QUALIT`: TECQUA, DEFECT, NODATA, SATURA, CLOLOW merged
        - :code:`CLASSI`: CLOUDS and SNOICE **only with :code:`00` as a band !**

        Args:
            mask_id (Union[str, S2GmlMasks]): Mask ID
            band (Union[BandNames, str]): Band number as an SpectralBandNames or str (for clouds: 00)
            resolution (int): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.

        Returns:
            gpd.GeoDataFrame: Mask as a DataArray
        """
        # Check inputs
        mask_id = S2Jp2Masks.from_value(mask_id)
        if mask_id == S2Jp2Masks.CLOUDS:
            band = "00"

        # Get QI_DATA path
        if isinstance(band, BandNames):
            band_id = self.bands[band].id
        else:
            band_id = band

        if self.is_archived:
            mask_path = files.get_archived_rio_path(
                self.path, f".*GRANULE.*QI_DATA.*MSK_{mask_id.value}_B{band_id}.jp2"
            )
        else:
            # Get mask path
            mask_path = files.get_file_in_dir(
                self.path,
                f"**/*GRANULE/*/QI_DATA/MSK_{mask_id.value}_B{band_id}.jp2",
                exact_name=True,
            )

        # Read mask
        mask = utils.read(
            mask_path,
            resolution=resolution,
            size=size,
            resampling=Resampling.nearest,
            **kwargs,
        )

        return mask

    def _manage_invalid_pixels(
        self, band_arr: xr.DataArray, band: BandNames, **kwargs
    ) -> xr.DataArray:
        """
        Manage invalid pixels (Nodata, saturated, defective...)
        See there: https://sentinel.esa.int/documents/247904/349490/S2_MSI_Product_Specification.pdf

        Args:
            band_arr (xr.DataArray): Band array
            band (BandNames): Band name as an SpectralBandNames
            kwargs: Other arguments used to load bands

        Returns:
            xr.DataArray: Cleaned band array
        """
        if self._processing_baseline < 4.0:
            return self._manage_invalid_pixels_lt_4_0(band_arr, band, **kwargs)
        else:
            # return band_arr
            return self._manage_invalid_pixels_gt_4_0(band_arr, band, **kwargs)

    def _manage_nodata(
        self, band_arr: xr.DataArray, band: BandNames, **kwargs
    ) -> xr.DataArray:
        """
        Manage only nodata pixels

        Args:
            band_arr (xr.DataArray): Band array
            band (BandNames): Band name as an SpectralBandNames
            kwargs: Other arguments used to load bands

        Returns:
            xr.DataArray: Cleaned band array
        """
        if self._processing_baseline < 4.0:
            return self._manage_nodata_lt_4_0(band_arr, band, **kwargs)
        else:
            return self._manage_nodata_gt_4_0(band_arr, band, **kwargs)

    def _manage_invalid_pixels_lt_4_0(
        self, band_arr: xr.DataArray, band: BandNames, **kwargs
    ) -> xr.DataArray:
        """
        Manage invalid pixels (Nodata, saturated, defective...)
        See there: https://sentinel.esa.int/documents/247904/349490/S2_MSI_Product_Specification.pdf

        Args:
            band_arr (xr.DataArray): Band array
            band (BandNames): Band name as an SpectralBandNames
            kwargs: Other arguments used to load bands

        Returns:
            xr.DataArray: Cleaned band array
        """
        # Get detector footprint to deduce the outside nodata
        nodata_det = self._open_mask_lt_4_0(
            S2GmlMasks.FOOTPRINT, band
        )  # Detector nodata, -> pixels that are outside of the detectors

        if len(nodata_det) > 0:
            # Rasterize nodata
            mask = features.rasterize(
                nodata_det.geometry,
                out_shape=(band_arr.rio.height, band_arr.rio.width),
                fill=self._mask_true,  # Outside detector = nodata (inverted compared to the usual)
                default_value=self._mask_false,  # Inside detector = not nodata
                transform=transform.from_bounds(
                    *band_arr.rio.bounds(), band_arr.rio.width, band_arr.rio.height
                ),
                dtype=np.uint8,
            )
        else:
            # Manage empty geometry: nodata is 0
            LOGGER.warning(
                "Empty detector footprint (DETFOO) vector. Nodata will be set where the pixels are null."
            )
            s2_nodata = 0
            mask = np.where(band_arr == s2_nodata, 1, 0).astype(np.uint8)

        #  Load masks and merge them into the nodata
        nodata_pix = self._open_mask_lt_4_0(
            S2GmlMasks.NODATA, band
        )  # Pixel nodata, not pixels that are outside of the detectors !!!
        if len(nodata_pix) > 0:
            # Discard pixels corrected during crosstalk
            nodata_pix = nodata_pix[nodata_pix.gml_id == "QT_NODATA_PIXELS"]
        nodata_pix.append(self._open_mask_lt_4_0(S2GmlMasks.DEFECT, band))
        nodata_pix.append(self._open_mask_lt_4_0(S2GmlMasks.SATURATION, band))

        # Technical quality mask
        tecqua = self._open_mask_lt_4_0(S2GmlMasks.QUALITY, band)
        if len(tecqua) > 0:
            # Do not take into account ancillary data
            tecqua = tecqua[tecqua.gml_id.isin(["MSI_LOST", "MSI_DEG"])]
        nodata_pix.append(tecqua)

        if len(nodata_pix) > 0:
            # Rasterize mask
            mask_pix = features.rasterize(
                nodata_pix.geometry,
                out_shape=(band_arr.rio.height, band_arr.rio.width),
                fill=self._mask_false,  # Outside vector
                default_value=self._mask_true,  # Inside vector
                transform=transform.from_bounds(
                    *band_arr.rio.bounds(), band_arr.rio.width, band_arr.rio.height
                ),
                dtype=np.uint8,
            )

            mask[mask_pix] = self._mask_true

        return self._set_nodata_mask(band_arr, mask)

    def _manage_invalid_pixels_gt_4_0(
        self, band_arr: xr.DataArray, band: BandNames, **kwargs
    ) -> xr.DataArray:
        """
        Manage invalid pixels (Nodata, saturated, defective...)
        See there:
        https://sentinels.copernicus.eu/documents/247904/685211/Sentinel-2-Products-Specification-Document-14_8.pdf

        Args:
            band_arr (xr.DataArray): Band array
            band (BandNames): Band name as an SpectralBandNames
            kwargs: Other arguments used to load bands

        Returns:
            xr.DataArray: Cleaned band array
        """
        # Get detector footprint to deduce the outside nodata
        nodata = self._open_mask_gt_4_0(
            S2Jp2Masks.FOOTPRINT, band, size=(band_arr.rio.width, band_arr.rio.height)
        ).data

        nodata = np.where(nodata == 0, 1, 0).astype(np.uint8)

        # Manage quality mask
        # TODO: Optimize it -> very slow (why ?)
        # Technical quality mask: Only keep MSI_LOST (band 3) and MSI_DEG (band 4)
        # Defective pixels (band 5)
        # Nodata pixels (band 6)
        # Saturated pixels (band 8)
        quality = (
            self._open_mask_gt_4_0(
                S2Jp2Masks.QUALITY,
                band,
                size=(band_arr.rio.width, band_arr.rio.height),
                indexes=[3, 4, 5, 6, 8],
                masked=False,
            )
            .astype(np.uint8)
            .data
        )

        # Compute mask
        mask = (nodata + np.sum(quality, axis=0)) > 0

        return self._set_nodata_mask(band_arr, mask)

    def _manage_nodata_lt_4_0(
        self, band_arr: xr.DataArray, band: BandNames, **kwargs
    ) -> xr.DataArray:
        """
        Manage only nodata
        See there: https://sentinel.esa.int/documents/247904/349490/S2_MSI_Product_Specification.pdf

        Args:
            band_arr (xr.DataArray): Band array
            band (BandNames): Band name as an SpectralBandNames
            kwargs: Other arguments used to load bands

        Returns:
            xr.DataArray: Cleaned band array
        """
        # Get detector footprint to deduce the outside nodata
        nodata_det = self._open_mask_lt_4_0(
            S2GmlMasks.FOOTPRINT, band
        )  # Detector nodata, -> pixels that are outside of the detectors

        if len(nodata_det) > 0:
            # Rasterize nodata
            mask = features.rasterize(
                nodata_det.geometry,
                out_shape=(band_arr.rio.height, band_arr.rio.width),
                fill=self._mask_true,  # Outside detector = nodata (inverted compared to the usual)
                default_value=self._mask_false,  # Inside detector = not nodata
                transform=transform.from_bounds(
                    *band_arr.rio.bounds(), band_arr.rio.width, band_arr.rio.height
                ),
                dtype=np.uint8,
            )
        else:
            # Manage empty geometry: nodata is 0
            LOGGER.warning(
                "Empty detector footprint (DETFOO) vector. Nodata will be set where the pixels are null."
            )
            s2_nodata = 0
            mask = np.where(band_arr == s2_nodata, 1, 0).astype(np.uint8)

        return self._set_nodata_mask(band_arr, mask)

    def _manage_nodata_gt_4_0(
        self, band_arr: xr.DataArray, band: BandNames, **kwargs
    ) -> xr.DataArray:
        """
        Manage only nodata
        See there: https://sentinel.esa.int/documents/247904/349490/S2_MSI_Product_Specification.pdf

        Args:
            band_arr (xr.DataArray): Band array
            band (BandNames): Band name as an SpectralBandNames
            kwargs: Other arguments used to load bands

        Returns:
            xr.DataArray: Cleaned band array
        """
        # Get detector footprint to deduce the outside nodata
        nodata = self._open_mask_gt_4_0(
            S2Jp2Masks.FOOTPRINT, band, size=(band_arr.rio.width, band_arr.rio.height)
        ).data

        nodata = np.where(nodata == 0, 1, 0).astype(np.uint8)

        return self._set_nodata_mask(band_arr, nodata)

    def _load_bands(
        self,
        bands: list,
        resolution: float = None,
        size: Union[list, tuple] = None,
        **kwargs,
    ) -> dict:
        """
        Load bands as numpy arrays with the same resolution (and same metadata).

        Args:
            bands (list): List of the wanted bands
            resolution (float): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
            kwargs: Other arguments used to load bands
        Returns:
            dict: Dictionary {band_name, band_xarray}
        """
        # Return empty if no band are specified
        if not bands:
            return {}

        if resolution is None and size is not None:
            resolution = self._resolution_from_size(size)
        band_paths = self.get_band_paths(bands, resolution=resolution, **kwargs)

        # Open bands and get array (resampled if needed)
        band_arrays = self._open_bands(
            band_paths, resolution=resolution, size=size, **kwargs
        )

        return band_arrays

    def _get_condensed_name(self) -> str:
        """
        Get S2 products condensed name ({date}_S2_{tile}_{product_type}_{generation_time}).

        Returns:
            str: Condensed name
        """
        # Used to make the difference between 2 products acquired on the same tile at the same date but cut differently
        # Sentinel-2 generation time: "%Y%m%dT%H%M%S" -> save only %H%M%S
        gen_time = self.split_name[-1].split("T")[-1]
        return f"{self.get_datetime()}_{self.constellation.name}_{self.tile_name}_{self.product_type.name}_{gen_time}"

    @cache
    def get_mean_sun_angles(self) -> (float, float):
        """
        Get Mean Sun angles (Azimuth and Zenith angles)

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.get_mean_sun_angles()
            (149.148155074489, 32.6627897525474)

        Returns:
            (float, float): Mean Azimuth and Zenith angle
        """
        try:
            # Read metadata
            root, _ = self.read_mtd()

            try:
                mean_sun_angles = root.find(".//Mean_Sun_Angle")
                zenith_angle = float(mean_sun_angles.findtext("ZENITH_ANGLE"))
                azimuth_angle = float(mean_sun_angles.findtext("AZIMUTH_ANGLE"))
            except TypeError:
                raise InvalidProductError(
                    "Azimuth or Zenith angles not found in metadata!"
                )
        except InvalidProductError as ex:
            LOGGER.warning(f"{ex}: setting sun angles to (0, 0).")
            azimuth_angle = 0.0
            zenith_angle = 0.0

        return azimuth_angle, zenith_angle

    @cache
    def _read_mtd(self) -> (etree._Element, dict):
        """
        Read metadata and outputs the metadata XML root and its namespaces as a dict

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.read_mtd()
            (<Element {https://psd-14.sentinel2.eo.esa.int/PSD/S2_PDI_Level-2A_Tile_Metadata.xsd}Level-2A_Tile_ID at ...>,
            {'nl': '{https://psd-14.sentinel2.eo.esa.int/PSD/S2_PDI_Level-2A_Tile_Metadata.xsd}'})

        Returns:
            (etree._Element, dict): Metadata XML root and its namespaces
        """
        mtd_from_path = "GRANULE/*/*.xml"
        mtd_archived = r"GRANULE.*\.xml"

        return self._read_mtd_xml(mtd_from_path, mtd_archived)

    @cache
    def read_datatake_mtd(self) -> (etree._Element, dict):
        """
        Read datatake metadata and outputs the metadata XML root and its namespaces as a dict
        (datatake metadata is the file in the root directory named :code:`MTD_MSI(L1C/L2A).xml`)

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.read_mtd()
            (<Element {https://psd-14.sentinel2.eo.esa.int/PSD/S2_PDI_Level-2A_Tile_Metadata.xsd}Level-2A_Tile_ID at ...>,
            {'nl': '{https://psd-14.sentinel2.eo.esa.int/PSD/S2_PDI_Level-2A_Tile_Metadata.xsd}'})

        Returns:
            (etree._Element, dict): Metadata XML root and its namespaces
        """
        mtd_from_path = "MTD_MSI*.xml"
        mtd_archived = r"MTD_MSI.*\.xml"

        return self._read_mtd_xml(mtd_from_path, mtd_archived)

    def _has_cloud_band(self, band: BandNames) -> bool:
        """
        Does this products has the specified cloud band ?
        https://sentinels.copernicus.eu/web/sentinel/technical-guides/sentinel-2-msi/level-1c/cloud-masks
        """
        if band == SHADOWS:
            has_band = False
        else:
            has_band = True
        return has_band

    def _open_clouds_lt_4_0(
        self,
        bands: list,
        resolution: float = None,
        size: Union[list, tuple] = None,
        **kwargs,
    ) -> dict:
        """
        Load cloud files as xarrays.

        Read S2 cloud mask .GML files (both valid for L2A and L1C products).
        https://sentinels.copernicus.eu/web/sentinel/technical-guides/sentinel-2-msi/level-1c/cloud-masks

        Args:
            bands (list): List of the wanted bands
            resolution (int): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
            kwargs: Additional arguments
        Returns:
            dict: Dictionary {band_name, band_xarray}
        """
        band_dict = {}

        if bands:
            cloud_vec = self._open_mask_lt_4_0(S2GmlMasks.CLOUDS)

            # Open a bands to mask it
            def_band = self._read_band(
                self.get_default_band_path(),
                self.get_default_band(),
                resolution=resolution,
                size=size,
            )
            nodata = np.where(np.isnan(def_band), 1, 0)

            for band in bands:
                if band == ALL_CLOUDS:
                    cloud = self._rasterize(def_band, cloud_vec, nodata)
                elif band == CIRRUS:
                    try:
                        cirrus = cloud_vec[cloud_vec.maskType == "CIRRUS"]
                    except AttributeError:
                        # No masktype -> empty
                        cirrus = gpd.GeoDataFrame(geometry=[], crs=cloud_vec.crs)
                    cloud = self._rasterize(def_band, cirrus, nodata)
                elif band == CLOUDS:
                    try:
                        clouds = cloud_vec[cloud_vec.maskType == "OPAQUE"]
                    except AttributeError:
                        # No masktype -> empty
                        clouds = gpd.GeoDataFrame(geometry=[], crs=cloud_vec.crs)
                    cloud = self._rasterize(def_band, clouds, nodata)
                elif band == RAW_CLOUDS:
                    cloud = self._rasterize(def_band, cloud_vec, nodata)
                else:
                    raise InvalidTypeError(
                        f"Non existing cloud band for Sentinel-2: {band}"
                    )

                # Rename
                band_name = to_str(band)[0]

                # Multi bands -> do not change long name
                if band != RAW_CLOUDS:
                    cloud.attrs["long_name"] = band_name
                band_dict[band] = cloud.rename(band_name).astype(np.float32)

        return band_dict

    def _open_clouds_gt_4_0(
        self,
        bands: list,
        resolution: float = None,
        size: Union[list, tuple] = None,
        **kwargs,
    ) -> dict:
        """
        Load cloud files as xarrays.

        Read S2 cloud mask .JP2 files (both valid for L2A and L1C products).
        https://sentinels.copernicus.eu/documents/247904/685211/Sentinel-2-Products-Specification-Document-14_8.pdf

        Args:
            bands (list): List of the wanted bands
            resolution (int): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
            kwargs: Additional arguments
        Returns:
            dict: Dictionary {band_name, band_xarray}
        """
        band_dict = {}

        if bands:
            cloud_vec = self._open_mask_gt_4_0(
                S2Jp2Masks.CLOUDS, "00", resolution=resolution, size=size
            ).astype(np.uint8)

            for band in bands:
                if band == ALL_CLOUDS:
                    cloud = cloud_vec[0, :, :] | cloud_vec[1, :, :]
                elif band == CIRRUS:
                    cloud = cloud_vec[1, :, :]  # CIRRUS = band 2
                elif band == CLOUDS:
                    cloud = cloud_vec[0, :, :]  # OPAQUE = band 1
                elif band == RAW_CLOUDS:
                    cloud = cloud_vec
                else:
                    raise InvalidTypeError(
                        f"Non existing cloud band for Sentinel-2: {band}"
                    )

                if len(cloud.shape) == 2:
                    cloud = cloud.expand_dims(dim="band", axis=0)

                # Rename
                band_name = to_str(band)[0]

                # Multi bands -> do not change long name
                if band != RAW_CLOUDS:
                    cloud.attrs["long_name"] = band_name

                band_dict[band] = cloud.rename(band_name).astype(np.float32)

        return band_dict

    def _open_clouds(
        self,
        bands: list,
        resolution: float = None,
        size: Union[list, tuple] = None,
        **kwargs,
    ) -> dict:
        """
        Load cloud files as xarrays.

        Read S2 cloud mask .GML files (both valid for L2A and L1C products).
        https://sentinels.copernicus.eu/web/sentinel/technical-guides/sentinel-2-msi/level-1c/cloud-masks

        Args:
            bands (list): List of the wanted bands
            resolution (int): Band resolution in meters
            size (Union[tuple, list]): Size of the array (width, height). Not used if resolution is provided.
            kwargs: Additional arguments
        Returns:
            dict: Dictionary {band_name, band_xarray}
        """
        if self._processing_baseline < 4.0:
            return self._open_clouds_lt_4_0(bands, resolution, size, **kwargs)
        else:
            return self._open_clouds_gt_4_0(bands, resolution, size, **kwargs)

    def _rasterize(
        self, xds: xr.DataArray, geometry: gpd.GeoDataFrame, nodata: np.ndarray
    ) -> xr.DataArray:
        """
        Rasterize a vector on a memory dataset

        Args:
            xds (xr.DataArray): Array
            geometry (gpd.GeoDataFrame): Geometry to rasterize
            nodata (np.ndarray): Nodata mask

        Returns:
            xr.DataArray: Rasterized vector
        """
        if not geometry.empty:
            # Just in case
            if geometry.crs != xds.rio.crs:
                geometry = geometry.to_crs(xds.rio.crs)

            # Rasterize mask
            cond = features.rasterize(
                geometry.geometry,
                out_shape=(xds.rio.height, xds.rio.width),
                fill=self._mask_false,  # Pixels outside mask
                default_value=self._mask_true,  # Pixels inside mask
                transform=transform.from_bounds(
                    *xds.rio.bounds(), xds.rio.width, xds.rio.height
                ),
                dtype=np.uint8,
            )
            cond = np.expand_dims(cond, axis=0)

        else:
            # If empty geometry, just
            cond = np.full(
                shape=(xds.rio.count, xds.rio.height, xds.rio.width),
                fill_value=self._mask_false,
                dtype=np.uint8,
            )
        return self._create_mask(xds, cond, nodata)

    def _l2ap_geocode_data(
        self, path: Union[CloudPath, Path]
    ) -> (Affine, int, int, CRS):
        """"""
        try:
            # Read metadata
            root, ns = self.read_mtd()

            # Determie wanted resolution
            if "10m" in path.name:
                res = 10
            elif "20m" in path.name:
                res = 20
            else:
                res = 60

            # Open size
            width = int(root.findtext(f".//Size[@resolution='{res}']/NCOLS"))
            height = int(root.findtext(f".//Size[@resolution='{res}']/NROWS"))

            # Open upper-left corner
            ulx = float(root.findtext(f".//Geoposition[@resolution='{res}']/ULX"))
            uly = float(root.findtext(f".//Geoposition[@resolution='{res}']/ULY"))

            # Create transform
            tf = transform.from_origin(ulx, uly, res, res)
        except InvalidProductError as ex:
            raise InvalidProductError(f"{ex}: cannot geocode the bands!")

        return tf, width, height, self.crs()

    @cache
    def default_transform(self, **kwargs) -> (Affine, int, int, CRS):
        """
        Returns default transform data of the default band (UTM),
        as the :code:`rasterio.warp.calculate_default_transform` does:
        - transform
        - width
        - height
        - crs

        Args:
            kwargs: Additional arguments
        Returns:
            Affine, int, int: transform, width, height

        """
        if self._processing_baseline < 2.07:
            default_path = self.get_default_band_path(**kwargs)
            return self._l2ap_geocode_data(default_path)
        else:
            return super().default_transform()

    @cache
    def get_cloud_cover(self) -> float:
        """
        Get cloud cover as given in the metadata

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.get_cloud_cover()
            55.5

        Returns:
            float: Cloud cover as given in the metadata
        """
        # Get the cloud cover
        try:
            # Get MTD XML file
            root, nsmap = self.read_mtd()
            cc = float(root.findtext(".//CLOUDY_PIXEL_PERCENTAGE"))
        except (InvalidProductError, TypeError):
            LOGGER.warning(
                "CLOUDY_PIXEL_PERCENTAGE not found in metadata! Cloud coverage set to 0."
            )
            cc = 0

        return cc

    def get_quicklook_path(self) -> str:
        """
        Get quicklook path if existing (some providers are providing one quicklook, such as creodias)

        Returns:
            str: Quicklook path
        """
        quicklook_path = None
        try:
            if self.is_archived:
                quicklook_path = files.get_archived_rio_path(
                    self.path, file_regex=r".*ql\.jpg"
                )
            else:
                quicklook_path = str(next(self.path.glob("**/*ql.jpg")))
        except (StopIteration, FileNotFoundError):
            try:
                if self.is_archived:
                    quicklook_path = files.get_archived_rio_path(
                        self.path, file_regex=r".*preview\.jpg"
                    )
                else:
                    quicklook_path = str(next(self.path.glob("**/preview.jpg")))
            except (StopIteration, FileNotFoundError):
                # Use the TCI
                try:
                    if self.product_type == S2ProductType.L2A:
                        tci_regex = "TCI_60m"
                    else:
                        tci_regex = "TCI"
                    if self.is_archived:
                        quicklook_path = files.get_archived_rio_path(
                            self.path, file_regex=rf".*{tci_regex}\.jp2"
                        )
                    else:
                        quicklook_path = str(
                            next(self.path.glob(f"**/{tci_regex}.jp2"))
                        )
                except (StopIteration, FileNotFoundError):
                    LOGGER.warning(f"No quicklook found in {self.condensed_name}")

        return quicklook_path

    @cache
    def get_orbit_direction(self) -> OrbitDirection:
        """
        Get cloud cover as given in the metadata

        .. code-block:: python

            >>> from eoreader.reader import Reader
            >>> path = r"S2A_MSIL1C_20200824T110631_N0209_R137_T30TTK_20200824T150432.SAFE.zip"
            >>> prod = Reader().open(path)
            >>> prod.get_orbit_direction().value
            "DESCENDING"

        Returns:
            OrbitDirection: Orbit direction (ASCENDING/DESCENDING)
        """
        try:
            # Get MTD XML file
            root, _ = self.read_datatake_mtd()

            # Get the orbit direction
            try:
                od = OrbitDirection.from_value(
                    root.findtext(".//SENSING_ORBIT_DIRECTION")
                )

            except TypeError:
                raise InvalidProductError(
                    "SENSING_ORBIT_DIRECTION not found in metadata!"
                )
        except InvalidProductError:
            od = OrbitDirection.DESCENDING

        return od
