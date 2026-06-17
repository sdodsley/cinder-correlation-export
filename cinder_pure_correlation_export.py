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
# For volume-backed instances (where the root disk is also a volume), the
# attachment layout matters for recovery: the disks must be re-attached in the
# correct order and the boot volume identified correctly. This script therefore
# captures every attachment per volume, the device path (which encodes disk
# order), and a boot-volume marker derived from the instance's root_device_name
# (authoritative) or, when that is unavailable, a clearly-labelled heuristic.
#
# OUTPUTS (written to --output-dir each run, timestamped, with 'latest' links):
#   volume_correlation_<ts>.csv             one row per volume (flat)
#   volume_correlation_<ts>.json            one record per volume (full detail)
#   volume_correlation_by_instance_<ts>.json
#                                           per-instance recovery view: each
#                                           instance's disks ordered by device,
#                                           boot volume flagged - use this to
#                                           rebuild a VM with the correct disk
#                                           order.
#
# The script is READ-ONLY against OpenStack. It performs no mutating calls.
#
# Author: Simon Dodsley, Everpure
# Licence: Apache-2.0
#
# ---------------------------------------------------------------------------
# REQUIREMENTS
#   python3 -m pip install openstacksdk
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
import re
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


# The Pure Storage Cinder driver records the true backing-volume name in the
# volume's metadata under this key. This is the authoritative source and is
# used directly. The name is NOT constructed from the OpenStack volume id:
# after a migration or retype the backing name no longer matches
# volume-<id>-cinder, so any constructed value would be wrong.
PURE_NAME_METADATA_KEY = "array_volume_name"

# The driver also records which FlashArray the volume lives on, under this key.
# Capturing it disambiguates the correlation in multi-backend deployments,
# where the same volume name could otherwise exist on more than one array.
PURE_ARRAY_METADATA_KEY = "array_name"

# Used only by the offline --lookup reverse helper to parse a UUID back out of
# an array volume name. Names look like 'volume-<uuid>-cinder', optionally with
# a Purity pod/volume-group scope prefix ('mypod::volume-...').
PURE_NAME_PREFIX = "volume-"
PURE_NAME_SUFFIX = "-cinder"

LOG = logging.getLogger("correlation_export")

# Field order for the CSV export. JSON uses the same keys.
FIELDNAMES = [
    "export_timestamp_utc",
    "openstack_volume_uuid",
    "pure_volume_name",
    "pure_array_name",
    "pure_name_source",
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
    "is_boot_volume",
    "instance_root_device",
    "attachment_count",
    "all_attachments",
    "availability_zone",
    "created_at",
]


# A Pure backing volume name looks like 'volume-<uuid>-cinder', optionally
# carrying a Purity pod/volume-group scope prefix ('mypod::volume-...').
PURE_NAME_RE = re.compile(
    r"(?:[^\s:/]+::)?volume-[0-9a-fA-F-]{36}-cinder\Z"
)


def _volume_metadata(volume):
    """Return the volume's user metadata as a dict (empty if none)."""
    meta = getattr(volume, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def resolve_pure_volume_name(volume):
    """Return (pure_volume_name, source) by reading the true array volume name
    directly from volume metadata.

    The Pure driver records the real backing-volume name in metadata under
    'array_volume_name'. This is read verbatim. The name is deliberately NOT
    constructed from the OpenStack volume id: after a migration or retype the
    backing name diverges from volume-<id>-cinder, so a constructed value would
    be incorrect. If the field is absent, that is reported (empty name with a
    'missing-metadata' source) so it can be investigated rather than masked by
    a fabricated value.
    """
    value = _volume_metadata(volume).get(PURE_NAME_METADATA_KEY)
    if value:
        return value, "metadata:{0}".format(PURE_NAME_METADATA_KEY)
    return "", "missing-metadata"


def pure_array_name(volume):
    """Return the FlashArray name the volume resides on, read from metadata.

    Empty string if not recorded. Important in multi-backend deployments to
    disambiguate which array a given volume name belongs to.
    """
    return _volume_metadata(volume).get(PURE_ARRAY_METADATA_KEY, "") or ""


def uuid_from_pure_name(array_volume_name):
    """Parse the embedded UUID out of a Pure volume name. Returns None if the
    name does not match the driver pattern.

    Useful during recovery when starting from an orphaned array volume name.
    Handles names that may carry a pod/vgroup prefix such as 'mypod::volume-...'
    by isolating the final path component first.

    Note: for a migrated/retyped volume the embedded UUID is the volume's
    name_id, NOT its user-facing OpenStack id. To map an array name back to the
    OpenStack volume, match it against the 'pure_volume_name' column of an
    export rather than relying on the embedded UUID.
    """
    if not array_volume_name:
        return None
    # Strip any Purity pod or volume-group scope prefix ("pod::" / "vgroup/").
    name = array_volume_name.split("::")[-1].split("/")[-1]
    if name.startswith(PURE_NAME_PREFIX) and name.endswith(PURE_NAME_SUFFIX):
        return name[len(PURE_NAME_PREFIX):-len(PURE_NAME_SUFFIX)]
    return None


def connect(cloud_name):
    """Establish an OpenStack connection via env vars or a named cloud."""
    try:
        if cloud_name:
            conn = openstack.connect(cloud=cloud_name)
        else:
            conn = openstack.connect()
        # Touch the token early so auth failures surface here, not mid-run.
        conn.authorize()
        return conn
    except os_exc.SDKException as exc:
        LOG.error("Failed to connect/authenticate to OpenStack: %s", exc)
        raise


def get_server_info(conn, server_id, cache):
    """Return {'name', 'root_device'} for an instance, cached per server.

    root_device is the instance's root_device_name (e.g. '/dev/vda'), which is
    the authoritative way to identify the boot volume: the attachment whose
    device matches it is the boot disk. Resolved defensively, since it is an
    admin-only attribute exposed under varying keys; empty if unavailable.
    """
    if not server_id:
        return {"name": "", "root_device": ""}
    if server_id in cache:
        return cache[server_id]

    info = {"name": "", "root_device": ""}
    try:
        server = conn.compute.get_server(server_id)
        info["name"] = getattr(server, "name", "") or ""
        root = getattr(server, "root_device_name", None)
        if not root:
            try:
                raw = server.to_dict()
            except Exception:
                raw = {}
            for key, value in raw.items():
                if value and key.split(":")[-1] == "root_device_name":
                    root = value
                    break
        info["root_device"] = root or ""
    except os_exc.SDKException:
        # Instance may already be gone; identifiers from the volume remain.
        pass

    cache[server_id] = info
    return info


def extract_attachments(volume):
    """Return a list of attachment dicts for the volume, each with keys
    'server_id', 'device', and 'attachment_id'. Empty list if unattached.

    Captures ALL attachments (a volume may be multi-attached), preserving the
    device path which encodes the disk's position on the instance.
    """
    result = []
    for att in (getattr(volume, "attachments", None) or []):
        result.append({
            "server_id": att.get("server_id", "") or "",
            "device": att.get("device", "") or "",
            "attachment_id": att.get("attachment_id", "") or att.get("id", "") or "",
        })
    return result


def _device_sort_key(device):
    """Sort key that orders device paths by length then lexically, so
    /dev/vda < /dev/vdb < ... < /dev/vdz < /dev/vdaa rather than the naive
    lexical ordering that would misplace multi-letter suffixes.
    """
    return (len(device), device)


def classify_boot(device, root_device, volume_bootable):
    """Return one of 'yes', 'no', 'heuristic', 'unknown' for whether this
    attachment is the instance's boot disk.

    Authoritative when the instance root_device_name is known: an exact device
    match is the boot disk. When it is not known, fall back to a clearly
    labelled heuristic based on the conventional first-disk device names and
    the volume's bootable flag.
    """
    if root_device:
        return "yes" if device == root_device else "no"
    base = (device or "").rsplit("/", 1)[-1]
    if base in ("vda", "sda", "xvda", "hda") and volume_bootable:
        return "heuristic"
    if not volume_bootable:
        return "no"
    return "unknown"


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
    server_cache = {}
    rows = []

    volumes = conn.block_storage.volumes(
        details=True, all_projects=all_projects
    )
    for vol in volumes:
        pure_name, name_source = resolve_pure_volume_name(vol)
        bootable = bool(getattr(vol, "is_bootable", False))

        # Enrich every attachment with the resolved instance name, the
        # instance's root device, and a boot classification.
        attachments = extract_attachments(vol)
        enriched = []
        for att in attachments:
            sinfo = get_server_info(conn, att["server_id"], server_cache)
            enriched.append({
                "server_id": att["server_id"],
                "instance_name": sinfo["name"],
                "device": att["device"],
                "attachment_id": att["attachment_id"],
                "instance_root_device": sinfo["root_device"],
                "is_boot": classify_boot(
                    att["device"], sinfo["root_device"], bootable
                ),
            })

        # Primary attachment fields (first attachment) kept for the flat view.
        primary = enriched[0] if enriched else {
            "server_id": "", "instance_name": "", "device": "",
            "instance_root_device": "", "is_boot": "no",
        }
        # A volume is a boot volume if any of its attachments is the boot disk.
        boot_flags = [a["is_boot"] for a in enriched]
        if "yes" in boot_flags:
            is_boot_volume = "yes"
        elif "heuristic" in boot_flags:
            is_boot_volume = "heuristic"
        elif enriched and all(f == "no" for f in boot_flags):
            is_boot_volume = "no"
        elif enriched:
            is_boot_volume = "unknown"
        else:
            is_boot_volume = ""  # unattached

        rows.append({
            "export_timestamp_utc": now_iso,
            "openstack_volume_uuid": vol.id,
            "pure_volume_name": pure_name,
            "pure_array_name": pure_array_name(vol),
            "pure_name_source": name_source,
            "volume_display_name": getattr(vol, "name", "") or "",
            "volume_description": getattr(vol, "description", "") or "",
            "volume_status": getattr(vol, "status", "") or "",
            "volume_size_gb": getattr(vol, "size", "") or "",
            "volume_type": getattr(vol, "volume_type", "") or "",
            "bootable": bootable,
            "project_id": project_id_of(vol),
            "attached_instance_uuid": primary["server_id"],
            "attached_instance_name": primary["instance_name"],
            "attached_device": primary["device"],
            "is_boot_volume": is_boot_volume,
            "instance_root_device": primary["instance_root_device"],
            "attachment_count": len(enriched),
            "all_attachments": enriched,
            "availability_zone": getattr(vol, "availability_zone", "") or "",
            "created_at": getattr(vol, "created_at", "") or "",
        })

    LOG.info("Collected correlation data for %d volume(s).", len(rows))
    return rows


def build_instance_view(rows):
    """Pivot the per-volume rows into a per-instance recovery view.

    Returns a list of instances, each with its disks ordered by device path
    and the boot volume flagged, so a volume-backed VM can be rebuilt with the
    correct disk attachment order. Volumes with no attachment are skipped here
    (they still appear in the per-volume export).
    """
    instances = {}
    for row in rows:
        for att in row["all_attachments"]:
            sid = att["server_id"]
            if not sid:
                continue
            inst = instances.setdefault(sid, {
                "instance_uuid": sid,
                "instance_name": att["instance_name"],
                "instance_root_device": att["instance_root_device"],
                "disks": [],
            })
            inst["disks"].append({
                "device": att["device"],
                "is_boot": att["is_boot"],
                "openstack_volume_uuid": row["openstack_volume_uuid"],
                "pure_volume_name": row["pure_volume_name"],
                "pure_array_name": row["pure_array_name"],
                "volume_size_gb": row["volume_size_gb"],
                "volume_display_name": row["volume_display_name"],
            })

    for inst in instances.values():
        inst["disks"].sort(key=lambda d: _device_sort_key(d["device"]))

    return sorted(instances.values(), key=lambda i: i["instance_name"] or i["instance_uuid"])


def write_outputs(rows, output_dir):
    """Write timestamped exports: a per-volume CSV and JSON, plus a per-instance
    recovery view (JSON) grouping disks by instance in attachment order.
    Returns (csv_path, json_path, instance_json_path).
    """
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    csv_path = os.path.join(output_dir, "volume_correlation_{0}.csv".format(stamp))
    json_path = os.path.join(output_dir, "volume_correlation_{0}.json".format(stamp))
    instance_json_path = os.path.join(
        output_dir, "volume_correlation_by_instance_{0}.json".format(stamp)
    )

    # CSV cannot hold nested structures; serialise list/dict cells to a JSON
    # string so the per-attachment detail survives in flattened form.
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            flat = {
                k: (json.dumps(v, separators=(",", ":"))
                    if isinstance(v, (list, dict)) else v)
                for k, v in row.items()
            }
            writer.writerow(flat)

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, sort_keys=False)

    with open(instance_json_path, "w", encoding="utf-8") as fh:
        json.dump(build_instance_view(rows), fh, indent=2, sort_keys=False)

    # Maintain stable "latest" symlinks for convenience.
    for target, link_name in (
        (csv_path, "volume_correlation_latest.csv"),
        (json_path, "volume_correlation_latest.json"),
        (instance_json_path, "volume_correlation_by_instance_latest.json"),
    ):
        link_path = os.path.join(output_dir, link_name)
        try:
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.remove(link_path)
            os.symlink(os.path.basename(target), link_path)
        except OSError as exc:
            LOG.warning("Could not update symlink %s: %s", link_path, exc)

    LOG.info("Wrote %s, %s and %s", csv_path, json_path, instance_json_path)
    return csv_path, json_path, instance_json_path


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
        LOG.error("Export failed: %s", exc)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
