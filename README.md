# Planet Fitness US (Unofficial)

A Home Assistant integration for Planet Fitness in the US. It shows how busy your club is right now and how busy it usually gets, tracks your check-ins like the app's My Progress, and can show your membership billing. It reads from the same service as the Planet Fitness app, signed in as you.

> **Not affiliated with, endorsed by or sponsored by Planet Fitness.** See [Disclaimer](#disclaimer).

## Requirements

- Home Assistant 2026.10 or later.
- A Planet Fitness membership at a US club, and the email address you use for the app.

## Install

[![Open this repository in the Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=sharpsan&repository=planet_fitness_ha&category=integration)

**With the Marketplace (Home Assistant 2026.11 or later):** open the link above, or go to **Settings → Marketplace → Add from link** and enter `sharpsan/planet_fitness_ha`. Then install **Planet Fitness US (Unofficial)** from its page.

**With HACS (2026.10):** add `https://github.com/sharpsan/planet_fitness_ha` as a custom repository (type *Integration*), install **Planet Fitness US (Unofficial)**, then restart Home Assistant.

**By hand:** copy `custom_components/planet_fitness` into your Home Assistant `config/custom_components/` folder, then restart.

## Set up

[![Add the integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=planet_fitness)

1. Open the link above, or go to **Settings → Devices & services → Add integration** and pick **Planet Fitness US (Unofficial)**.
2. Enter the email address you use for the Planet Fitness app.
3. Enter the code Planet Fitness emails you.

Your home club and your membership show up as devices. To follow another club, open the integration, choose **Add club**, enter a ZIP code or move the pin near it, and pick it from the nearest clubs. To stop following one, delete its entry under the integration.

If Planet Fitness ever signs you out, Home Assistant asks you to sign in again the same way.

## Entities

| Device | Entities |
| --- | --- |
| Each club | Crowd level, Estimated people, Crowd status, Typical crowd now, Busier than usual, Quietest time today, Crowd trend, Open, Next opening, Next closing, Capacity |
| Membership | Last check-in, Check-ins this week, Check-ins this month, Check-ins this year, Total check-ins, Weekly streak, Checked in, Checked in today, Check-in (event), Visits (calendar) |
| Membership, disabled by default | Member since, Next payment amount, Next payment date, Monthly fee, Annual fee, Balance due, Payment overdue |

- **Open** follows the club's regular and holiday hours, so it switches at opening and closing time, not at the next update. Its attributes list the hours and any holiday hours in the next 30 days.
- **Typical crowd now** has a `today` attribute with all 24 hours, for charts.
- **Check-in** fires once for each new visit, with `club` and `check_in_time`, when it shows up in your history (checked hourly and as soon as you're checked in, so usually within an hour). Visits from before Home Assistant started (or the integration reloaded) don't fire it, except one still under way (up to a visit length before the start) that shows up later. Nor do visits more than 2 days old, or (once it's known) from before your join date.
- **Checked in** is on while Planet Fitness shows you as checked in.
- **Last check-in** has a `club` attribute. **Monthly fee** has `billing_day`, and **Annual fee** has `next_billing_date`.
- After the first setup, Planet Fitness is only asked for your fees while Next payment amount, Next payment date, Monthly fee or Annual fee is on, and for your balance while Balance due or Payment overdue is on. Turning one on loads it within a minute.
- The check-in counts show unknown, not a number that could drop later, so long-term statistics stay right. They wait for your join date (Member since), and for Planet Fitness to return any visits (it sometimes leaves history out for a while; Weekly streak shows 0 meanwhile). For a new member, they're unknown until the first visit. Check-ins this year and Total check-ins also wait until all the history they cover has loaded.
- Once your join date is known, visits from before it (a past membership) are left out. Visits already loaded are kept through later gaps.
- Weeks run Sunday to Saturday, like the app. **Weekly streak** counts weeks in a row with a visit; the current week doesn't break it until it ends.

## Options

- **Moderate from / High from:** the crowd levels where Crowd status changes (25% and 50% by default).
- **Busier than usual at:** Busier than usual turns on when the crowd is more than this many times the typical crowd for the hour (1.25 by default), and more than 5 points above it.
- **Visit length:** how long each check-in lasts in the Visits calendar, and how long a visit under way when Home Assistant starts still counts for Check-in (60 minutes by default).

## How it works

- It only reads. Nothing on your account changes.
- It checks crowd levels every 5 minutes (up to 30 while a club is closed), whether you're checked in every 10 minutes, your recent check-ins hourly (sooner once you're checked in), the last year of check-ins daily (hourly while there are none), the typical crowd daily, and billing every 12 hours. When it starts, it loads every year of check-ins once. If Planet Fitness asks it to slow down, it waits at least as long as asked (up to a day), and its entities show as unavailable from their next check until the first one after that.
- If the typical crowd, check-in history or billing returns an error, or the typical crowd comes back without a capacity, the last values stay. It tries again after 1, 2, 4… hours, up to its usual schedule. Recent check-ins are retried every hour, and so is membership info while your join date is unknown. The log gets one warning when that starts and, with info logging on, one note when the data is back.
- If a club can't be read, its entities are unavailable until the next check. If your checked-in status can't be read, all the membership entities are. Until check-in history first loads, Last check-in, the counts, Weekly streak and Checked in today are unavailable.
- Estimated people is the crowd level times the club's capacity, so it's approximate.
- It keeps only the sign-in tokens Planet Fitness issues, not your email. Diagnostics leave out your sign-in tokens, visit history, billing amounts, and the names and locations of your clubs.

## Remove

Delete it under **Settings → Devices & services → Planet Fitness US (Unofficial)**, which also deletes its sign-in tokens. Then uninstall it from its page in the Marketplace or HACS (or delete the `custom_components/planet_fitness` folder) and restart Home Assistant. Nothing changes on your Planet Fitness account.

## Disclaimer

This is an independent, unofficial project. It is not affiliated with, endorsed by or sponsored by Planet Fitness or any of its franchisees. "Planet Fitness" and the PF logo are trademarks of their owner. They're used here only to show which service this integration works with; the icon is a modified version of the logo, marked unofficial.

The integration relies on the private API used by the Planet Fitness app, which can change or stop working at any time. Use it at your own risk and within your membership terms. It is provided as is, without warranty of any kind.
