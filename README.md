# Vela

Self-hosted cabin player for a Tesla browser. Vela plays **YouTube** and **Plex** on the passenger screen while the car is moving.

**Passenger use only.** Watching video on the driver's display while the car is moving is illegal and dangerous in most places. This project does not change that.

Vela is an original interface. It is not TeslaPlay and it does not reuse that project's branding.

## Why it works in Drive

Tesla's browser pauses a normal `<video>` element (and `drawImage(video)`, which comes out black) once the car leaves Park. Vela never uses a `<video>` element for the picture.

1. The server resolves the real stream (yt-dlp or Plex) and remuxes it to fragmented MP4.
2. The browser demuxes that file with mp4box.
3. **WebCodecs** decodes H.264 frames and paints them on a `<canvas>`.
4. AAC is decoded and played with the **Web Audio API**.

H.264 is the reliable high-quality path. 1080p is the default. **Max** plays the best H.264 stream it can copy, including above 1080p when YouTube or Plex already has one.

If the page is not HTTPS, WebCodecs is unavailable. Vela then falls back to a JSMpeg canvas (MPEG-1). That path works on plain HTTP, looks worse, and uses a lot of CPU on the NAS. Put Vela behind HTTPS before you care about quality.

## Hardware notes

This is aimed at an older TrueNAS host (Haswell i7-4790K, 32 GB). Copying H.264 is cheap. Re-encoding is not.

| Source | What Vela does |
| --- | --- |
| YouTube H.264 + AAC | Remux. Video is copied. |
| YouTube VP9/AV1 only | Transcode to H.264 on the NAS with `veryfast`, capped at 1080p. |
| Plex file that is already H.264 at the chosen size | Remux from the Plex file URL. |
| Plex HEVC, or H.264 that must be scaled down | Ask **Plex** to transcode (so a Plex Pass hardware session can do it), then remux the HLS output. |

Only two ffmpeg jobs run at once.

## Quick start

```bash
cp .env.example .env
# set APP_PASSWORD, PLEX_URL, PLEX_TOKEN
docker compose up -d --build
```

Open `http://<host>:8095`, sign in, then put the site behind HTTPS before using it in the car.

## Who can sign in

Vela has two seats and no more. Nginx Proxy Manager and Cloudflare only carry the connection. They are not the login. Sign-in is built into Vela, in the same style as Organizr's Plex login and TeslaPlay's Google login.

| Seat | How they get in |
| --- | --- |
| Owner | Cabin password (`APP_PASSWORD`), Plex, or Google |
| Grok test | Username `grok` plus its password, or a Plex/Google account you attach to that seat |

Nobody else can get in. A Plex or Google account is refused until that seat lists the username or email. The first person to click the button is not granted access.

**Plex** is the one to use in the car. The screen shows a 4-character code (and a QR). On your phone, open `plex.tv/link` and approve it. The Tesla browser never sees the Plex password, which matters when the Plex account itself is "Sign in with Google" — Google often blocks the car browser.

**Password** still works. Leave the username blank for the owner cabin password. For the test seat, choose Tester sign-in and use username `grok`.

**Google** is optional and meant for a laptop, not the car. Set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. In Settings, copy the redirect URI (`https://<your-domain>/api/auth/google/callback`) into the Google OAuth client. Set `PUBLIC_URL` to that same https origin so the redirect stays stable behind Nginx Proxy Manager.

Linking is done while you are already signed in as the owner: Settings → Link Plex to this seat. You can also set `OWNER_EMAIL` and `OWNER_PLEX_USERNAME` before the first start. Those only fill empty fields. After that, Settings is in charge.

If `TEST_PASSWORD` is empty, the first start writes a password to `/config/test-user` and shows it once in Settings. Treat that file like `APP_PASSWORD`.

The test seat can play. It cannot change the Plex token, YouTube cookies, or the allow list.

If `APP_PASSWORD` is empty, the first start writes one to `/config/generated-password`. Prefer setting the variable yourself. Read a generated password with:

```bash
docker exec tesla-media-player cat /config/generated-password
```

### YouTube Premium cookies

Higher bitrate and ad-free playback need **your** YouTube cookies. Vela does not ship any account.

1. In a desktop browser, sign in to YouTube.
2. Export cookies in Netscape format (`cookies.txt`) with a cookies extension.
3. Either place the file at `config/youtube-cookies.txt`, or upload it from Vela's Settings page.

The container path is `/config/youtube-cookies.txt`. Treat that file like a password. It is not committed.

YouTube changes its player often. The image includes current `yt-dlp` plus [Deno](https://github.com/yt-dlp/yt-dlp/wiki/EJS) so the solver can run. If search starts failing, rebuild the image so yt-dlp updates.

### Plex

Settings, or environment variables:

| Variable | Purpose |
| --- | --- |
| `PLEX_URL` | Base URL reachable **from the container**, for example `http://192.168.1.42:32400` |
| `PLEX_TOKEN` | Plex token. Find it via a Plex XML request while signed in (`X-Plex-Token` in the browser network panel). |
| `DEFAULT_QUALITY` | `480`, `720`, `1080`, or `max` |

The Tesla never sees the Plex token or the YouTube CDN URL. It only talks to Vela.

Libraries of type movie and show are listed, including separate 4K libraries. Shows open into seasons and episodes.

### Quality

In the player: **480**, **720**, **1080**, **Max**.

- **1080** is the one to leave on.
- **Max** on YouTube is the highest H.264 rendition (often 1080p; sometimes higher). If the only higher formats are VP9/AV1, Vela transcodes to 1080p instead of melting the CPU on a 4K encode.
- **Max** on a Plex H.264 file remuxes the original, which may be 4K if that is what the file is. HEVC is still sent through Plex and capped at 1080p H.264.

**Compatibility** forces the MPEG-1 canvas path. Use it only when WebCodecs misbehaves. It is pinned around 720p on purpose.

### HTTPS

WebCodecs requires a secure context (HTTPS, or localhost). A Tesla cannot use localhost on your NAS.

**Homelab (recommended):** Nginx Proxy Manager.

- Forward to `http://<nas>:8095`
- Websockets support: on (compatibility mode needs it)
- Force SSL
- In the advanced tab:

```nginx
proxy_buffering off;
proxy_request_buffering off;
```

Without `proxy_buffering off`, NPM will hold the stream and the picture will sit on "loading".

**Standalone:** Caddy is in the optional `https` profile.

```bash
# .env
VELA_DOMAIN=vela.example.com
docker compose --profile https up -d
```

Caddy terminates TLS and proxies to Vela with flushing enabled. For a real name, point DNS at the host. `localhost` uses Caddy's internal certificate, which the car will not trust.

## Configuration reference

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_PASSWORD` | generated file | Owner cabin password |
| `OWNER_EMAIL` | empty | Fills the owner's Plex and Google email when those are still blank |
| `OWNER_PLEX_USERNAME` | empty | Fills the owner's allowed Plex username when blank |
| `TEST_USERNAME` | `grok` | Test seat username, used on first create |
| `TEST_PASSWORD` | generated file | Test seat password. Empty writes `/config/test-user` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | empty | Optional Google sign-in |
| `PUBLIC_URL` | request host | Public https origin, used for the Google redirect |
| `PUID` / `PGID` | `568` | TrueNAS apps user |
| `TZ` | `America/Toronto` | |
| `PORT` | `8095` | Inside the container |
| `CONFIG_DIR` | `/config` | Settings, cookies, generated password |
| `YOUTUBE_COOKIES` | `/config/youtube-cookies.txt` | |
| `PLEX_URL` | empty | |
| `PLEX_TOKEN` | empty | |
| `DEFAULT_QUALITY` | `1080` | |

`settings.json` in the config volume stores the Plex URL, token, and default quality if you change them in the UI. Environment values are the defaults the first time. A non-empty token typed into Settings replaces the env token.

## Ports

Host **8095** → container **8095**. Chosen so it does not collide with SABnzbd on 8080.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
CONFIG_DIR=./config APP_PASSWORD=dev python -m uvicorn server.main:app --host 0.0.0.0 --port 8095
python -m unittest tests.test_units
```

Node 22 or Deno must be on `PATH` for YouTube extraction outside Docker. The image already has Deno.

## Image

Published from GitHub Actions to:

```text
ghcr.io/adsouza98/tesla-media-player:0.2.0
ghcr.io/adsouza98/tesla-media-player:latest
```

The workflow targets `linux/amd64` (Haswell). If the package is created private, make it public once:

```bash
gh api --method PUT -H "Accept: application/vnd.github+json" \
  /user/packages/container/tesla-media-player/visibility \
  -f visibility=public
```

## Layout

```text
server/     FastAPI, yt-dlp, Plex, ffmpeg
web/        Cabin UI, WebCodecs player, JSMpeg fallback
docker/     Entrypoint that drops to PUID:PGID
```

## Safety

- Passenger use only.
- Set a password. Vela can reach your Plex server and your YouTube cookies.
- Do not commit `config/`, `.env`, or `youtube-cookies.txt`.
- Do not expose port 8095 to the internet without HTTPS and the password.
