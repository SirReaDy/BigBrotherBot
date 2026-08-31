# Bundled data

## `dbip-country-lite.mmdb`

IP-to-country data from [DB-IP](https://db-ip.com/), the **IP to Country Lite** database, so that
`geolocation` — and the `location`, `countryfilter` and `welcome` plugins that read what it publishes
— work on a fresh install with nothing to download and no account to register.

> IP Geolocation by DB-IP — <https://db-ip.com>

Licensed **CC BY 4.0**: <https://creativecommons.org/licenses/by/4.0/>. Redistributing it here is
what that licence allows, and the attribution above is what it asks for in return. It is also why
this file and not MaxMind's GeoLite2, which is free to download but may not be redistributed.

**It goes stale, and a stale answer is a confident wrong one** — an address that changed provider
answers with the old country. DB-IP publish monthly; `b3 init` fetches the current file so a new
instance starts with fresh data, and re-running it refreshes an existing one. This copy is the floor,
not the ceiling: it is what an install has when it cannot reach the network.

Country only. For a city, a region or the network's operator, point `database` at MaxMind's
GeoLite2-City or GeoLite2-ASN instead — see `examples/plugin_geolocation.yaml`.
