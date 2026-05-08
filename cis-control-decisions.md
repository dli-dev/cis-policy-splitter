# CIS v4.0.0 Control Decisions

**Date:** 2026-03-31 | **Author:** Derek Liu (DITA CoP)

Reconciled from committee review, deployment plan, and follow-up research.

---

## Exceptionable — 20 controls (scope tag 001)

### Settings Catalog (14)

```
CIS #            Setting                         Baseline              Alternatives
26.7 + 49.8      Screen Lock Inactivity           15 min                30 min, Disabled
49.1             Guest Account Status             Disabled              Enabled
49.4             Rename Guest Account             Renamed               Not renamed
4.4.2            SMB v1 client driver             Disabled              Enabled
4.6.9.1          Network Bridge                   Prohibited            Allowed
4.10.9.1.3       IEEE 1394 device setup           Blocked               Allowed (COSU)
4.10.9.2         Device Metadata Retrieval        Blocked               Allowed
4.10.26.2        Network selection UI             Hidden                Visible (COSU)
4.11.7.3.1       Deny Write to Unencrypted USB    Required              Allowed (write to unencrypted)
4.11.36.4.2.1    Allow RDP Connections            Disabled              Enabled
68.2             Input Personalization            Block                 Allow
89.10            Create Symbolic Links            Admins                Admins + Hyper-V
89.12            Debug Programs                   Admins                Admins + Debugger Users
89.14            Deny Local Log On                Guests                None (COSU)
```

### System Service Scripts (6)

```
CIS #   Service                          Baseline = Disable    Exception = exclude (stays running)
81.13   iSCSI Initiator (MSiSCSI)        Disable               Storage needs
81.14   OpenSSH SSH Server (sshd)         Disable               Remote admin
81.17   Remote Desktop Config (SessionEnv) Disable              RDP needed (pair with 81.18 + 4.11.36.4.2.1)
81.18   Remote Desktop Services (TermService) Disable           RDP needed (pair with 81.17 + 4.11.36.4.2.1)
81.23   Routing and Remote Access          Disable              COSU routing needs
```

---

## Reject — strip from policies (12 + entire Windows Update JSON)

```
CIS #            Setting                                  Reason
55.5             Disable Store Originated Apps            Breaks Company Portal app deployment
76.1.2           Notify Password Reuse                    Breaks WebLogin; pending WHfB migration
80.5             Disable OneDrive Sync                    OneDrive required; researching tenant-scoped restrictions
81.6             Geolocation Service                      Geolocation needed; researching compensating controls
12.1             Allow Camera = Not allowed               Blocks Teams/Zoom video
81.1             Bluetooth Audio Gateway (BTAGService)    Breaks wireless peripherals; no Intune Bluetooth version controls
81.2             Bluetooth Support (bthserv)              Breaks wireless peripherals; no Intune Bluetooth version controls
103.1-103.6      Entire Windows Update JSON               WUfB managed via Update Rings, not Settings Catalog
```

---

## Modified Value — deploy with non-CIS value (8)

```
CIS #         Setting                                CIS Value                        Our Value
49.29         UAC standard user elevation prompt     Automatically deny requests      Prompt for credentials on Secure Desktop
4.11.7.2.9    Require additional auth at startup     Enabled (paired w/ require-PIN)  Enabled (TPM-only required; PIN/key/PIN+key forbidden)
4.11.7.2.12   Configure TPM startup PIN              Require startup PIN with TPM     Do not allow startup PIN with TPM
4.11.7.2.13   Configure TPM startup                  Do not allow TPM                 Require TPM
A.4.19        ASR: Office apps → child procs         Audit                            Block
A.4.20        ASR: Office comm apps → child procs    Audit                            Block
A.4.21        ASR: Win32 API calls from Office macro Block                            Audit (revisit after 30–60d telemetry)
A.4.22        ASR: Obfuscated JS/VBS scripts         Audit                            Block
```

49.29: non-exceptionable, scope tag 001. Using LAPS account for elevation.

4.11.7.2.9 / .12 / .13: previously rejected wholesale because CIS pairs the parent toggle with require-PIN, which breaks Autopilot/silent BitLocker. Reframed: keep the parent enabled (so devices can't downgrade to no startup auth), but require TPM-only and forbid PIN/key/PIN+key/non-TPM. The three currently-untracked siblings (TPM+key, TPM+PIN+key, non-TPM) accept upstream CIS `_0` (Do not allow) by default and so are not separately listed in cis-control-config.json.

A.4.19 / .20 / .22: ASR rules where LHS sets Block and CIS L1 - Defender ships Audit. Decision tracker A.4 (2026-05-07) keeps LHS block-mode for all three.

A.4.21 (Block Win32 API calls from Office macros, rule GUID 92E97FA1-2EDF-4476-BDD6-9DD0B4DDDC7B): goes the other direction — CIS ships Block and UofT picks Audit for the 30–60 day telemetry window before flipping to Block. Implemented as a CIS-source modification (rather than an override policy that fights ASR most-restrictive-wins conflict resolution). After audit review, swap `modifiedValue` to `_block` to flip.

---

## Not Applicable — strip from policies (7)

```
CIS #            Setting                                              Reason
4.11.7.1.5       BitLocker fixed drive: configure storage to AD DS    No on-prem AD DS
4.11.7.2.5       BitLocker OS drive: configure storage to AD DS       No on-prem AD DS
4.11.7.2.6       BitLocker OS drive: don't enable until stored to AD  Would block BitLocker — no AD DS to store to
4.11.7.2.8       BitLocker OS drive: save recovery info to AD DS      No on-prem AD DS
```

### Accept (parent toggle + safe children)

```
CIS #            Setting                                              Reason
4.11.7.2.1       BitLocker OS drive recovery = Enabled                Parent toggle needed for Entra key escrow
4.11.7.1.4       Allow data recovery agent = True                     General setting, not AD DS specific
4.11.7.1.6       Don't enable until stored to AD (fixed) = False      Safe — value means don't wait
```

---

## Resolved from Research — accept into baseline

```
CIS #            Setting                                  Finding
4.6.17.1         Windows Connect Now configuration        Does not affect normal Wifi connections — only WPS/push-button provisioning
4.6.17.2         Prohibit Windows Connect Now access      Does not affect normal Wifi connections — only WPS/push-button provisioning
4.10.20.1.1      Turn off access to the Store             Only removes Store from "Open With" dialog; does not block Store app or Company Portal
4.10.26.7        Convenience PIN sign-in                  Not WHfB PIN — convenience PIN caches password locally, no TPM. Safe to disable.
89.31            Remote Shutdown = Administrators         Intune/SCCM restarts run as SYSTEM, not subject to user rights. No impact.
49.5             Prevent users from installing printers    Does not affect DUA printing
```

---

## Autopilot — user group targeting

```
CIS #   Setting                          Note
4.5.1   AutoAdminLogon = Disabled        Must target user group to avoid breaking pre-provisioning
26.5    Password History = 24            Must target user group
49.28   UAC admin elevation prompt       Must target user group
90.1    HVCI = Enabled with UEFI lock    Must target user group; delayed application
```

---

## Do Not Deploy Yet

```
CIS #       Setting                      Blocker
49.9        Logon message text           Pending legal review of message content
49.10       Logon message title          Pending legal review of message content
```

---

## Accept with Specific Value

```
CIS #          Setting                                    CIS Allows         We Deploy
4.11.7.4       BitLocker encryption: fixed drives          128 or 256 bit     256 bit
4.11.7.5       BitLocker encryption: OS drives             128 or 256 bit     256 bit
4.11.7.6       BitLocker encryption: removable drives      128 or higher      256 bit
```

---

## Conditionally Accepted

```
CIS #   Setting                              Condition
49.6    Do not display last signed-in        Accept now; revisit when implementing Windows Hello
81.34   WpnService (push notifications)      Accept and deploy; monitor for Intune push notification issues
```
