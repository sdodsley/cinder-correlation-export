# cinder-correlation-export

A read-only tool that exports a durable mapping between **OpenStack Cinder volumes** and their **Pure Storage FlashArray** backing volumes — so that when a volume is deleted in OpenStack but still exists on the array, you can correlate the orphaned array volume back to its original VM, owner, and disk layout during a recovery.

It is designed for clouds that are **volume-backed** (including the root disk), where a recovery must re-attach disks in the correct order and identify the boot volume correctly.

> Read-only by design: the script makes **no** mutating calls against OpenStack. It only lists volumes and servers.

## The problem it solves

OpenStack and the array each hold half of the picture, for different lengths of time:

- **OpenStack knows the meaning** — which volume belongs to which VM, who owns it, what it's called, how it's attached. But the Cinder DB record and its metadata are removed the moment a volume is deleted.
- **The FlashArray keeps the object** — with `pure_eradicate_on_delete = False` (the default), a deleted volume is retained in a pending-eradication state for the array's eradication period (24h by default), so it remains recoverable for a while.

During a recovery you have the surviving array object but not its meaning. This tool captures the meaning **before** deletion and retains it outside OpenStack, bridging the gap.

## Why not just build the name from the volume UUID?

The FlashArray Cinder driver names backing volumes `volume-<uuid>-cinder`, so it's tempting to construct that string from the OpenStack volume ID. **This is wrong for migrated or retyped volumes.**

The embedded UUID is the volume's `name_id`, not its user-facing ID. After a migration/retype, Cinder sets `name_id` to the temporary destination volume's UUID while the user-facing ID stays the same:

```
OpenStack volume ID : 30475d3a-82ee-4057-9a03-61a36359c050
Real backing name   : volume-95578c91-4c03-4854-9698-17e87226d8d1-cinder
```

In a maturing cloud most volumes have been migrated, so a constructed name would be *silently wrong* for most of them — exactly during the recovery you were preparing for.

This tool **reads the truth instead of guessing**: the driver records the real backing-volume name in volume metadata under `array_volume_name`, and the array it lives on under `array_name`. Both survive migrations and are read verbatim. Where the field is genuinely absent, the tool reports an empty value flagged `missing-metadata` rather than fabricating a name.

## What it captures

Per volume:

- OpenStack volume UUID
- True FlashArray backing name (`array_volume_name`) and the array it resides on (`array_name`)
- `pure_name_source` — provenance of the resolved name, so every correlation is auditable
- Display name, description, status, size, type, bootable flag, project/tenant
- **Full attachment detail** — every attachment, the device path, the attached instance UUID/name
- **Boot-volume identification** and the instance's root device

### Boot volume & disk order

For volume-backed VMs, attaching the wrong disk as root means a failed boot, and disks attached out of order can corrupt filesystems. The tool identifies the boot disk authoritatively by matching each attachment's device against the instance's `root_device_name`. When that attribute isn't visible (depends on your role), it falls back to a clearly-labelled heuristic (conventional first-disk device names + bootable flag), marking the result `heuristic` rather than `yes` so you always know whether the call is fact or inference.

## Outputs

Each run writes timestamped files to `--output-dir`, plus stable `*_latest.*` symlinks:

| File | Contents |
| --- | --- |
| `volume_correlation_<ts>.csv` | One row per volume (flat). Nested attachment detail is JSON-encoded into a cell. |
| `volume_correlation_<ts>.json` | One record per volume, full detail including the per-attachment list. |
| `volume_correlation_by_instance_<ts>.json` | **Per-instance recovery view**: each VM's disks sorted into attachment order with the boot volume flagged — the artifact to rebuild from. |

Exports older than `--retention-days` are pruned automatically.

## Requirements

- Python 3
- [`openstacksdk`](https://pypi.org/project/openstacksdk/)

```bash
python3 -m pip install openstacksdk
```

## Authentication

Uses the standard `openstacksdk` auth chain — either source an admin `openrc` (`OS_AUTH_URL`, `OS_USERNAME`, …) or configure a `clouds.yaml` and pass `--cloud <name>`.

An **admin-capable role is recommended**: it's required for `--all-projects`, and it's what makes the boot-volume identification authoritative (visibility of `root_device_name`).

## Usage

```bash
# Export across all projects (default) using a sourced openrc
python3 cinder_pure_correlation_export.py --output-dir /var/lib/everpure/correlation

# Use a named cloud from clouds.yaml, keep 14 days of exports
python3 cinder_pure_correlation_export.py --cloud prod --retention-days 14

# Restrict to the authenticated project only
python3 cinder_pure_correlation_export.py --single-project

# Reverse lookup: given an orphaned FlashArray volume name, print the embedded UUID (offline)
python3 cinder_pure_correlation_export.py --lookup volume-95578c91-4c03-4854-9698-17e87226d8d1-cinder
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--output-dir` | `/var/lib/everpure/correlation` | Where timestamped exports are written. |
| `--retention-days` | `7` | Prune exports older than this. `0` disables pruning. Keep it ≥ your array's eradication period. |
| `--cloud` | env vars | Named cloud from `clouds.yaml`. If omitted, `OS_*` env vars are used. |
| `--all-projects` / `--single-project` | all projects | Scope of the volume listing. All projects requires admin. |
| `--lookup <name>` | — | Offline reverse lookup: print the UUID embedded in a FlashArray volume name and exit. |
| `--verbose` | off | Debug logging. |

### Run it on a schedule

```cron
0 * * * * /usr/bin/python3 /opt/everpure/cinder_pure_correlation_export.py \
    --output-dir /var/lib/everpure/correlation --retention-days 7 \
    >> /var/log/everpure/correlation_export.log 2>&1
```

## Recovering a volume

1. Find the deleted volume on the FlashArray (it's in the **Destroyed** list while pending eradication); the name still carries its `volume-<uuid>-cinder` identifier.
2. Correlate it back to its VM and disk position using the most recent export — match the array name in the `pure_volume_name` column to recover the OpenStack UUID, owner, device, and boot flag. (For a migrated volume the UUID *embedded* in the name is the `name_id`, so match on the column rather than the embedded value.)
3. Recover the volume on the array (`purevol recover`).
4. Re-import it into OpenStack with `cinder manage`.
5. Re-attach to a new or rebuilt VM, using the per-instance view to preserve disk order and boot device.

## Caveats

- **Recovery depends on retention.** `pure_eradicate_on_delete` must be `False` for the eradication window to exist. Verify this on your production backends.
- **Boot identification needs admin visibility.** If `is_boot_volume` reads `heuristic` across the board, run the export with an admin-capable role so `root_device_name` is visible, and sanity-check a known VM before relying on it for a real rebuild.
- **It's a point-in-time snapshot.** Run it on a schedule that comfortably outpaces your deletion churn, and retain exports at least as long as the array's eradication period.

## License

Apache-2.0.
