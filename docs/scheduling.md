# Scheduling

The app can open a profile on a schedule, from the **Schedules** screen or the
[REST API](api.md#schedules). The scheduler lives inside the `phasmid-pm`
process (no cron, no extra service), because that process owns the browser
sessions: a scheduled launch goes through the same path as pressing *Run*, so
the lease, the exit-address lookup, the running-browsers list, the
close-on-window-close handling and the usage log all behave the same.

## What can be scheduled

- **Open the browser**, for account warming or any regular session. The launch
  uses this instance's default display mode: a virtual display when `PPM_VNC` is
  on (the Docker image), a desktop window otherwise. If the profile's browser is
  already running when the schedule fires, the run is recorded as *skipped* and
  nothing is touched. An optional *close after N minutes* ends the session by
  itself, so a 03:00 warming run does not leave a window open until someone
  notices. A run fails, and says why, when the launch would: for example a proxy
  that does not answer, or another instance holding the profile.

There is no scheduled browser update. Phasmid reports the version it was built
from, so every profile reports whichever release is installed; installing a
newer release updates them all at once, as on a real machine.

## What deliberately cannot be scheduled

**Regenerating the hardware fingerprint.** The pinned machine exists so that a
profile is *the same computer* every session — that guarantee is the core of
this product (see [profile-settings.md](profile-settings.md)), and the
real-browser tests check that a page sees the same machine on the next launch.
Rotating the hardware on a timer would undo it: a site that has seen one GPU,
one screen and one core count for months would suddenly see another at 3am,
which is precisely the signal a long-lived account must never emit.

Regenerating stays available as a deliberate, manual action — *Regenerate
fingerprint* on the profile, which warns about what it costs.

## How schedules fire

- **Two expressions.** *Every N minutes*, or *daily at HH:MM* on chosen
  weekdays. Not cron: the two forms cover warming, they can be
  displayed back unambiguously, and there is no syntax to get wrong.
- **Whose clock?** The server's — the machine running `phasmid-pm`. In the
  normal single-machine setup that is also your clock. If you run the server
  remotely, "09:00" is 09:00 *there*.
- **Missed runs are skipped, not replayed.** If the app was closed when a run
  was due, the gap is recorded in the run history as one *missed* entry and the
  schedule waits for its next occurrence. A warming launch is only meaningful
  at its time, and replaying a night's backlog would open every missed browser
  at once on startup.
- **Failures don't stop the scheduler.** Each run is recorded — *ok*,
  *skipped*, *error* or *missed*, with a message — and one failing schedule
  never blocks another. A schedule whose profile has been deleted disables
  itself. The last 20 runs are kept per schedule.
