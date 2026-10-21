# Chromium singleton recovery after Docker recreation

Chromium persists `session/SingletonLock -> hostname-pid` in LocalAuth's
profile. Docker recreation changes the hostname; Chromium can exit with code
21 even though the old container is gone. Missing PIDs inside another container
are not evidence that an owner is dead. The old explicit RESET path is not a
recovery mechanism: it deletes authentication data.

The host launcher now attempts non-destructive repair after stopping the app,
before the setup wizard or app recreation. It acquires the existing exclusive
`wa_session/.owner.lock`, verifies a local Unix-socket Docker endpoint, requires
a Docker-ID-shaped foreign hostname, and inspects all containers. A running,
paused or restarting owner, or any live overlapping data mount, blocks repair.
A stopped owner or an ID absent from a successful full inventory permits repair
only after checking visible host processes for the profile. Inspection errors,
unreadable processes, symlink profiles and unexpected singleton file types fail
closed. Only the three singleton symlinks are unlinked, never their targets.
Credentials, profile contents and the owner-lock inode are preserved.

Runtime and setup retain their existing exclusive lock. No Docker socket is
mounted into containers. No in-container PID guessing or automatic RESET is
introduced. Arbitrary custom hostnames and remote Docker contexts are not
automatically recoverable. As with the existing advisory lock protocol, direct
Chromium launches or external Docker managers must not race the launcher;
the launcher serializes its own operations, not arbitrary Docker API clients.

## Deployment

No running containers were changed while implementing this fix. At a planned
maintenance window, use the updated host launcher for setup/configure or up.
These operations stop the old app before repair; setup/configure rebuilds the
image, while up uses the existing image. Rebuild separately if deploying other
application changes. Do not remove `wa_session`, revoke the linked device or
choose RESET to resolve this stale-lock problem. Keep a private backup of the
data directory before maintenance. If repair is refused, investigate the owner,
Docker context, mounts and processes rather than deleting locks manually.

Offline regressions: `test_whatsapp_profile.py`; launcher regressions:
`test_wizard_fixes.py` and `test_installer.py`.