# Historical Zephyr Tester patches

These patches record the Phase 3 comparison experiments. They are retained for
provenance and do not participate in the default firmware build.

The active build uses the integrated commit in the dedicated
`VIDLG/zephyr` fork. No local patch is applied during the default build.
The consolidated patch is retained in this directory only as historical
provenance. The integrated fork commit is
`b64b351b49c027a1c56cb234f99538e168be315a`; unlike the old patch, it preserves
current main's handle lookup and status return, and accepts combined CCC bits.

Historical sequence:

1. `zephyr-v4.4.2-tester-indication-single-subscriber.patch` — Indication-only target selection;
2. `zephyr-v4.4.2-tester-set-value-status.patch` — native update status propagation;
3. `zephyr-v4.4.2-tester-update-single-subscriber.patch` — Notification and Indication target selection plus status propagation;
4. `zephyr-v4.4.2-tester-single-subscriber-database-lifecycle.patch` — consolidated update/status changes plus service slot and capacity corrections.

All patches target the upstream Zephyr Bluetooth Tester application
`tests/bluetooth/tester/src/btp_gatt.c`. They do not modify AutoPTS or the BTP
wire protocol.
