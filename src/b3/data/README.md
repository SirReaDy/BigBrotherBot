# Bundled data

## `dbip-country-lite.mmdb`

IP-to-country data from [DB-IP](https://db-ip.com/), the **IP to Country Lite** database, so that
`geolocation` — and the `location`, `countryfilter` and `welcome` plugins that read what it publishes
— work on a fresh install with nothing to download and no account to register.

> IP Geolocation by DB-IP — <https://db-ip.com>

Licensed **CC BY 4.0**: <https://creativecommons.org/licenses/by/4.0/>. Redistributing it here is
what that licence allows, and the attribution above is what it asks for in return — which is the
whole reason it is this database and not one of the licensed ones, several of which are free to
download but may not be passed on.

**It goes stale, and a stale answer is a confident wrong one** — an address that changed provider
answers with the old country. DB-IP publish monthly; `b3 init` fetches the current file so a new
instance starts with fresh data, and re-running it refreshes an existing one. This copy is the floor,
not the ceiling: it is what an install has when it cannot reach the network.

Country only. `b3 init` also fetches DB-IP's ASN file, which is what `!isp` reports; their free city
edition adds cities, regions and coordinates and is a download you make yourself, because it is 58 MB
against this file's 3. See `examples/plugin_geolocation.yaml`.
