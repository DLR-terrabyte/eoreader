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
"""
SPOT-6 products.
See `here <https://earth.esa.int/eogateway/documents/20142/37627/SPOT-6-7-imagery-user-guide.pdf>`_
for more information.
"""
import logging

from eoreader.bands import SpectralBand
from eoreader.bands import spectral_bands as spb
from eoreader.products import DimapProduct
from eoreader.reader import Constellation
from eoreader.stac import GSD, ID, NAME, WV_MAX, WV_MIN
from eoreader.utils import EOREADER_NAME

LOGGER = logging.getLogger(EOREADER_NAME)


class SpotProduct(DimapProduct):
    """
    Class of SPOT-6/7 products.
    See `here <https://earth.esa.int/eogateway/documents/20142/37627/SPOT-6-7-imagery-user-guide.pdf>`_
    for more information.
    """

    def _pre_init(self, **kwargs) -> None:
        """
        Function used to pre_init the products
        (setting needs_extraction and so on)
        """
        self._pan_res = 1.5
        self._ms_res = 6.0

        # Post init done by the super class
        super()._pre_init(**kwargs)

    def _get_constellation(self) -> Constellation:
        """ Getter of the constellation """
        sat_id = self.split_name[0]
        return getattr(Constellation, sat_id)

    def _map_bands(self) -> None:
        """Set products type"""
        # Create spectral bands
        pan = SpectralBand(
            eoreader_name=spb.PAN,
            **{NAME: "PAN", ID: 1, GSD: 1.5, WV_MIN: 455, WV_MAX: 744}
        )

        blue = SpectralBand(
            eoreader_name=spb.BLUE,
            **{NAME: "BLUE", ID: 1, GSD: 6, WV_MIN: 454, WV_MAX: 519}
        )

        green = SpectralBand(
            eoreader_name=spb.GREEN,
            **{NAME: "GREEN", ID: 2, GSD: 6, WV_MIN: 527, WV_MAX: 587}
        )

        red = SpectralBand(
            eoreader_name=spb.RED,
            **{NAME: "RED", ID: 3, GSD: 6, WV_MIN: 624, WV_MAX: 694}
        )

        nir = SpectralBand(
            eoreader_name=spb.NIR,
            **{NAME: "NIR", ID: 4, GSD: 6, WV_MIN: 760, WV_MAX: 900}
        )
        self._map_bands_core(blue=blue, green=green, red=red, nir=nir, pan=pan)
