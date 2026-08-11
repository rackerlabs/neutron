# Copyright 2026 Red Hat, LLC
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import dataclasses

from neutron_lib.db import api as db_api
from oslo_db import exception as os_db_exc
from oslo_log import log as logging
import sqlalchemy as sa

from neutron.db.models import vxlan_vlan_allocations as alloc_models
from neutron.db import rangeallocator

LOG = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ScopedRange:
    """An inclusive integer range and the physnet scoping its uniqueness.

    Field names mirror RangeAllocator.allocate(min_val, max_val, scope_val).
    """

    min_val: int
    max_val: int
    physnet: str


class VNIVLANAllocator:
    """Allocates paired VNI + VLAN IDs and manages their mapping.

    This is a generic allocator that can be used by any component needing
    a VNI/VLAN pair. It owns the full lifecycle of the vni_allocations,
    vlan_allocations, and vni_vlan_mapping rows.

    The VNI and the VLAN are allocated from independent pools, each given
    as its own ScopedRange. These are distinct concepts: a VNI is unique
    across the whole fabric, while a VLAN ID is only unique on a single
    physical network. A mapping may therefore pair a VNI in one physnet
    with a VLAN in another, and the same VLAN ID may be reused for many
    VNIs as long as the VLAN physnets differ.

    Callers provide exception classes so that errors are domain-specific.
    """

    def __init__(self, vni_exhausted_exc, vlan_exhausted_exc, vni_in_use_exc):
        """Initialize the allocator with two RangeAllocators.

        :param vni_exhausted_exc: Exception class raised when VNI range is
            exhausted. Must accept (min_val, max_val) positional args.
        :param vlan_exhausted_exc: Exception class raised when VLAN range is
            exhausted. Must accept (min_val, max_val) positional args.
        :param vni_in_use_exc: Exception class raised when a specific VNI
            is already allocated. Must accept vni= keyword arg.
        """
        self._vni_in_use_exc = vni_in_use_exc
        self._vni_allocator = rangeallocator.RangeAllocator(
            table=alloc_models.VNIAllocation.__table__,
            value_col_name='vni',
            scope_col_name='physnet',
            scope_param_type=sa.String,
            exception_class=vni_exhausted_exc,
        )
        self._vlan_allocator = rangeallocator.RangeAllocator(
            table=alloc_models.VLANAllocation.__table__,
            value_col_name='vlan_id',
            scope_col_name='physnet',
            scope_param_type=sa.String,
            exception_class=vlan_exhausted_exc,
        )

    @db_api.retry_if_session_inactive()
    @db_api.CONTEXT_WRITER
    def allocate(self, context, vni_range, vlan_range):
        """Auto-allocate a VNI and VLAN pair, creating the mapping.

        :param context: Neutron request context (with active session)
        :param vni_range: ScopedRange for the VNI pool
        :param vlan_range: ScopedRange for the VLAN pool
        :returns: (mapping_id, vni, vlan_id)
        """
        vni_alloc_id, vni = self._vni_allocator.allocate(
            context, vni_range.min_val, vni_range.max_val, vni_range.physnet)

        mapping_id, vlan_id = self._create_mapping(
            context, vni_alloc_id, vlan_range)

        LOG.debug("Allocated VNI %s on physnet %s / VLAN %s on physnet %s "
                  "(mapping %s)", vni, vni_range.physnet, vlan_id,
                  vlan_range.physnet, mapping_id)
        return mapping_id, vni, vlan_id

    @db_api.retry_if_session_inactive()
    @db_api.CONTEXT_WRITER
    def allocate_specific_vni(self, context, vni, vni_physnet, vlan_range):
        """Allocate a specific VNI and auto-allocate a VLAN, creating mapping.

        The VNI is given as a bare value and physnet rather than a
        ScopedRange because a caller-chosen VNI has no range.

        :param context: Neutron request context (with active session)
        :param vni: The specific VNI to allocate
        :param vni_physnet: Physical network scoping the VNI pool
        :param vlan_range: ScopedRange for the VLAN pool
        :returns: (mapping_id, vni, vlan_id)
        :raises: vni_in_use_exc if the VNI is already allocated
        """
        vni_allocation = alloc_models.VNIAllocation(
            vni=vni, physnet=vni_physnet)
        context.session.add(vni_allocation)

        try:
            # Flush to trigger the UNIQUE constraint check immediately
            # so we can catch the duplicate and raise a domain exception.
            context.session.flush()
        except os_db_exc.DBDuplicateEntry:
            raise self._vni_in_use_exc(vni=vni)

        mapping_id, vlan_id = self._create_mapping(
            context, vni_allocation.id, vlan_range)

        LOG.debug("Allocated specific VNI %s on physnet %s / VLAN %s on "
                  "physnet %s (mapping %s)", vni, vni_physnet, vlan_id,
                  vlan_range.physnet, mapping_id)
        return mapping_id, vni, vlan_id

    def _create_mapping(self, context, vni_alloc_id, vlan_range):
        """Allocate a VLAN and create a VNI-VLAN mapping row.

        :returns: (mapping_id, vlan_id)
        """
        vlan_alloc_id, vlan_id = self._vlan_allocator.allocate(
            context, vlan_range.min_val, vlan_range.max_val,
            vlan_range.physnet)

        mapping = alloc_models.VNIVLANMapping(
            vni_allocation_id=vni_alloc_id,
            vlan_allocation_id=vlan_alloc_id)
        context.session.add(mapping)
        context.session.flush()

        return mapping.id, vlan_id

    @db_api.retry_if_session_inactive()
    @db_api.CONTEXT_WRITER
    def deallocate(self, context, mapping_id):
        """Remove a VNI/VLAN mapping and its allocation rows.

        Deletes the mapping row (CASCADE removes any child rows like
        evpn_l3_instances), then deletes both allocation rows.
        Safe to call if the mapping does not exist.

        :param context: Neutron request context (with active session)
        :param mapping_id: ID of the vni_vlan_mapping row
        """
        mapping = context.session.query(
            alloc_models.VNIVLANMapping
        ).filter_by(id=mapping_id).first()
        if not mapping:
            return

        vni_alloc_id = mapping.vni_allocation_id
        vlan_alloc_id = mapping.vlan_allocation_id

        context.session.query(
            alloc_models.VNIVLANMapping
        ).filter_by(id=mapping_id).delete(synchronize_session=False)

        context.session.query(
            alloc_models.VNIAllocation
        ).filter_by(id=vni_alloc_id).delete(synchronize_session=False)

        context.session.query(
            alloc_models.VLANAllocation
        ).filter_by(id=vlan_alloc_id).delete(synchronize_session=False)

        LOG.debug("Deallocated mapping %s (VNI alloc %s, VLAN alloc %s)",
                  mapping_id, vni_alloc_id, vlan_alloc_id)
