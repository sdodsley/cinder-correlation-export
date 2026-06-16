#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# cinder_pure_correlation_export.py
#
# Periodically exports a durable mapping between OpenStack Cinder volumes and
# their corresponding Pure Storage FlashArray backing volumes, so that an
# orphaned (deleted-but-retained) FlashArray volume can be correlated back to
# its original OpenStack VM and owner during a recovery operation.
#
# The OpenStack Cinder database record and its metadata are removed at the time
# a volume is deleted. The FlashArray volume, by contrast, is retained in a
# pending-eradication state for the array's configured eradication period
# (24 hours by default, when pure_eradicate_on_delete = False). This script
# captures the human-meaningful context (VM name, owner, display name) BEFORE
# deletion and retains it outside OpenStack, bridging the recovery gap.
#
# The script is READ-ONLY against OpenStack. It performs no mutating calls.
#
# Author: Simon Dodsley, Everpure
# Licence: Apache-2.0
#
# ---------------------------------------------------------------------------
# REQUIREMENTS
#   python3 -m pip install 'openstacksdk>=0.103'
#
#   NOTE: cross-project volume listing via the `all_projects` query parameter
#   requires a reasonably recent openstacksdk. On older releases the kwarg is
#   silently ignored and only the authenticated project's volumes are returned.
#
# AUTHENTICATION
#   Uses the standard openstacksdk auth chain. Either:
#     a) Source an admin openrc file (OS_AUTH_URL, OS_USERNAME, etc.), or
#     b) Configure a clouds.yaml and pass --cloud <name>.
#   Admin (or a role able to list all projects) is required for --all-projects.
#
# TYPICAL CRON ENTRY (hourly, retain 7 days):
#   0 * * * * /usr/bin/python3 /opt/everpure/cinder_pure_correlation_export.py \
#       --output-dir /var/lib/everpure/correlation --retention-days 7 \
#       >> /var/log/everpure/correlation_export.log 2>&1
# ---------------------------------------------------------------------------

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

try:
    import openstack
    from openstack import exceptions as os_exc
except ImportError:
    sys.stderr.write(
        "ERROR: openstacksdk is not installed. Run: "
        "python3 -m pip install openstacksdk\n"
    )
    sys.exit(2)


# The Pure Storage Cinder driver names every backing volume as:
#   volume-<openstack-volume-uuid>-cinder
# These affixes are used to derive the FlashArray volume name from the
# OpenStack UUID, and (in reverse) to parse a UUID out of an array volume name
# during recovery.
PURE_NAME_PREFIX = "volume-"
PURE_NAME_SUFFIX = "-cinder"

LOG = logging.getLogger("correlation_export")

# Field order for the CSV export. JSON uses the same keys.
FIELDNAMES = [
    "export_timestamp_utc",
    "openstack_volume_uuid",
    "pure_volume_name",
    "volume_display_name",
    "volume_description",
    "volume_status",
    "volume_size_gb",
    "volume_type",
    "bootable",
    "project_id",
    "attached_instance_uuid",
    "attached_instance_name",
    "attached_device",
    "availability_zone",
    "created_at",
]


def pure_volume_name(volume_uuid):
    """Return the FlashArray volume name the Pure Cinder driver would assign.

    Mirrors the driver's naming convention: volume-<uuid>-cinder.
    """
    return "{0}{1}{2}".format(PURE_NAME_PREFIX, volume_uuid, PURE_NAME_SUFFIX)


def uuid_from_pure_name(array_volume_name):
    """Reverse of pure_volume_name(): extract the OpenStack UUID from a Pure
    volume name. Returns None if the name does not match the driver pattern.

    Useful during recovery when starting from an orphaned array volume name.
    Handles names that may carry a pod/vgroup prefix such as 'mypod::volume-...'
    by isolating the final path component first.
    """
    if not array_volume_name:
        return None
    # Strip any Purity pod or volume-group scope prefix ("pod::" / "vgroup/").
    name = array_volume_name.split("::")[-1].split("/")[-1]
    if name.startswith(PURE_NAME_PREFIX) and name.endswith(PURE_NAME_SUFFIX):
        uuid = name[len(PURE_NAME_PREFIX):-len(PURE_NAME_SUFFIX)]
        # Reject a name with an empty UUID body ("volume--cinder").
        return uuid or None
    return None


def _str_attr(volume, attr):
    """Read an attribute as a string, distinguishing 'absent' from a falsy
    value. Returns "" only when the attribute is missing or None; preserves
    False / 0 so that, e.g., a non-bootable volume records 'False' not ''.
    """
    value = getattr(volume, attr, None)
    if value is None:
        return ""
    return value


def connect(cloud_name):
    """Establish an OpenStack connection via env vars or a named cloud."""
    try:
        if cloud_name:
            conn = openstack.connect(cloud=cloud_name)
        else:
            conn = openstack.connect()
        # Touch the token early so auth failures surface here, not mid-run.
        # Auth failures raise keystoneauth1 exceptions, which do NOT subclass
        # os_exc.SDKException, so catch broadly to log a useful message.
        conn.authorize()
        return conn
    except Exception as exc:
        LOG.error("Failed to connect/authenticate to OpenStack: %s", exc)
        raise


def build_server_name_cache(conn, all_projects):
    """Pre-fetch server UUID -> name to avoid a per-volume compute lookup.

    Returns a dict keyed by server UUID. Falls back gracefully if the compute
    listing is not permitted; individual lookups will then be attempted later.
    """
    cache = {}
    try:
        for server in conn.compute.servers(
            details=False, all_projects=all_projects
        ):
            cache[server.id] = server.name
        LOG.info("Cached %d server name(s).", len(cache))
    except os_exc.SDKException as exc:
        LOG.warning(
            "Could not bulk-list servers (%s). Will resolve names "
            "individually where possible.", exc
        )
    return cache


def resolve_server_name(conn, server_id, cache):
    """Resolve an instance UUID to its name, using and updating the cache."""
    if not server_id:
        return ""
    if server_id in cache:
        return cache[server_id]
    try:
        server = conn.compute.get_server(server_id)
        cache[server_id] = server.name
        return server.name
    except os_exc.SDKException:
        # Instance may already be gone; the UUID is still recorded.
        cache[server_id] = ""
        return ""


def extract_attachment(volume):
    """Return (instance_uuid, device) for the first attachment, or ('', '').

    Cinder volumes may have multiple attachments (multi-attach); the first is
    sufficient for correlation. Extend here if multi-attach detail is needed.
    """
    attachments = getattr(volume, "attachments", None) or []
    if not attachments:
        return "", ""
    first = attachments[0]
    # openstacksdk returns attachment dicts with 'server_id' and 'device'.
    return first.get("server_id", "") or "", first.get("device", "") or ""


def project_id_of(volume):
    """Return the owning project/tenant ID across SDK attribute variants."""
    for attr in ("project_id", "tenant_id", "os-vol-tenant-attr:tenant_id"):
        value = getattr(volume, attr, None)
        if value:
            return value
    # Some SDK versions expose it via a location object.
    location = getattr(volume, "location", None)
    if location is not None:
        proj = getattr(location, "project", None)
        if isinstance(proj, dict):
            return proj.get("id", "") or ""
    return ""


def collect_rows(conn, all_projects):
    """Walk all Cinder volumes and build the correlation rows."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    server_cache = build_server_name_cache(conn, all_projects)
    rows = []

    volumes = conn.block_storage.volumes(
        details=True, all_projects=all_projects
    )
    for vol in volumes:
        instance_uuid, device = extract_attachment(vol)
        instance_name = resolve_server_name(conn, instance_uuid, server_cache)

        rows.append({
            "export_timestamp_utc": now_iso,
            "openstack_volume_uuid": vol.id,
            "pure_volume_name": pure_volume_name(vol.id),
            "volume_display_name": _str_attr(vol, "name"),
            "volume_description": _str_attr(vol, "description"),
            "volume_status": _str_attr(vol, "status"),
            "volume_size_gb": _str_attr(vol, "size"),
            "volume_type": _str_attr(vol, "volume_type"),
            "bootable": _str_attr(vol, "is_bootable"),
            "project_id": project_id_of(vol),
            "attached_instance_uuid": instance_uuid,
            "attached_instance_name": instance_name,
            "attached_device": device,
            "availability_zone": _str_attr(vol, "availability_zone"),
            "created_at": _str_attr(vol, "created_at"),
        })

    LOG.info("Collected correlation data for %d volume(s).", len(rows))
    return rows


def write_outputs(rows, output_dir):
    """Write timestamped CSV and JSON exports. Returns (csv_path, json_path)."""
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    csv_path = os.path.join(output_dir, "volume_correlation_{0}.csv".format(stamp))
    json_path = os.path.join(output_dir, "volume_correlation_{0}.json".format(stamp))

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, sort_keys=False)

    # Maintain a stable "latest" symlink for convenience.
    for target, link_name in (
        (csv_path, "volume_correlation_latest.csv"),
        (json_path, "volume_correlation_latest.json"),
    ):
        link_path = os.path.join(output_dir, link_name)
        try:
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.remove(link_path)
            os.symlink(os.path.basename(target), link_path)
        except OSError as exc:
            LOG.warning("Could not update symlink %s: %s", link_path, exc)

    LOG.info("Wrote %s and %s", csv_path, json_path)
    return csv_path, json_path


def prune_old_exports(output_dir, retention_days):
    """Delete export files older than retention_days. Skips symlinks."""
    if retention_days <= 0:
        return
    cutoff = time.time() - (retention_days * 86400)
    removed = 0
    for entry in os.listdir(output_dir):
        if not entry.startswith("volume_correlation_"):
            continue
        if entry.endswith("_latest.csv") or entry.endswith("_latest.json"):
            continue
        path = os.path.join(output_dir, entry)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError as exc:
            LOG.warning("Could not remove %s: %s", path, exc)
    if removed:
        LOG.info("Pruned %d export(s) older than %d day(s).",
                 removed, retention_days)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Export an OpenStack-to-Pure Storage volume correlation "
                    "record for recovery operations."
    )
    parser.add_argument(
        "--output-dir", default="/var/lib/everpure/correlation",
        help="Directory to write timestamped CSV/JSON exports "
             "(default: /var/lib/everpure/correlation)."
    )
    parser.add_argument(
        "--retention-days", type=int, default=7,
        help="Delete exports older than this many days. "
             "Set 0 to disable pruning (default: 7). "
             "Keep this >= your FlashArray eradication period."
    )
    parser.add_argument(
        "--cloud", default=None,
        help="Named cloud from clouds.yaml. If omitted, OS_* environment "
             "variables are used."
    )
    parser.add_argument(
        "--all-projects", dest="all_projects", action="store_true",
        default=True,
        help="List volumes across all projects (default; requires admin)."
    )
    parser.add_argument(
        "--single-project", dest="all_projects", action="store_false",
        help="Restrict to the authenticated project only."
    )
    parser.add_argument(
        "--lookup", metavar="ARRAY_VOLUME_NAME", default=None,
        help="Reverse-lookup mode: given a FlashArray volume name, print the "
             "originating OpenStack volume UUID and exit. No export is written."
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging."
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Reverse-lookup mode is offline and needs no OpenStack connection.
    if args.lookup:
        uuid = uuid_from_pure_name(args.lookup)
        if uuid:
            print(uuid)
            return 0
        LOG.error("'%s' does not match the Pure Cinder naming pattern "
                  "(%s<uuid>%s).", args.lookup,
                  PURE_NAME_PREFIX, PURE_NAME_SUFFIX)
        return 1

    try:
        conn = connect(args.cloud)
    except Exception:
        return 2

    try:
        rows = collect_rows(conn, args.all_projects)
        write_outputs(rows, args.output_dir)
        prune_old_exports(args.output_dir, args.retention_days)
    except os_exc.SDKException as exc:
        LOG.error("Export failed (OpenStack error): %s", exc)
        return 1
    except OSError as exc:
        # makedirs/open/symlink failures (e.g. output dir not writable).
        LOG.error("Export failed (filesystem error): %s", exc)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())

