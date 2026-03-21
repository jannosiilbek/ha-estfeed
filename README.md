# Estfeed Energy Data

A Home Assistant custom integration that fetches electricity and gas metering data from the [Estfeed](https://estfeed.elering.ee) platform, plus real-time NordPool electricity spot prices for Estonia via the Elering API.

## Features

- **Building electricity** consumption (daily and monthly)
- **Building gas** consumption in m³ and kWh (daily and monthly)
- **Apartment gas share** calculated from your apartment/building area ratio
- **Electricity spot price** (current hour, NordPool Estonia)
- **Average electricity price** for the current month

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Search for "Estfeed Energy Data" and install
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/estfeed` folder into your Home Assistant `custom_components` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Estfeed Energy Data**
3. Enter your Estfeed API credentials:
   - **Client ID** — from Estfeed portal → Account & Settings → API Key
   - **Client Secret** — the secret shown when you created the API key
   - **Apartment area (m²)** — your apartment's area
   - **Total building area (m²)** — combined area of all apartments in the building

The apartment and building areas can be updated later via the integration's options flow.

## Sensors

| Sensor | Unit | Description |
|--------|------|-------------|
| Building Electricity Today | kWh | Today's building electricity consumption |
| Building Electricity This Month | kWh | This month's building electricity consumption |
| Building Gas Today | m³ | Today's building gas consumption |
| Building Gas This Month | m³ | This month's building gas consumption |
| Building Gas Energy Today | kWh | Today's building gas in energy units |
| Building Gas Energy This Month | kWh | This month's building gas in energy units |
| Apartment Gas Today | m³ | Your apartment's gas share (by area) |
| Apartment Gas This Month | m³ | Your apartment's monthly gas share |
| Apartment Gas Energy Today | kWh | Your apartment's gas share in energy units |
| Apartment Gas Energy This Month | kWh | Your apartment's monthly gas share in energy units |
| Electricity Spot Price | EUR/kWh | Current hour NordPool spot price |
| Electricity Avg Price This Month | EUR/kWh | Average spot price this month |

## API Rate Limiting

The Estfeed API allows 1 request per 5 seconds. The integration enforces a 6-second minimum interval between requests and polls once per hour by default.
