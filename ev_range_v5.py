"""
EV Range Modeller v5  —  EPA-Calibrated Physics
================================================
What's new in v5:

  [v5-1] EPA fueleconomy.gov vehicle lookup — replaces the generic class
          picker.  User navigates Year → Make → Model → Trim using the
          official DOE/EPA REST API (no key required).  Covers every BEV
          sold in the US from 2011 onwards.

  [v5-2] EPA calibration scalar — the physics engine now computes a
          correction factor so that at EPA test conditions (22 °C, no wind,
          flat, no HVAC) the model exactly reproduces the EPA city or
          highway kWh/100 mi rating for the specific trim selected.
          Real-world deltas (weather, grade, HVAC, SOH) are then layered
          on top of that calibrated baseline for much more accurate results.

  [v5-3] Auto battery capacity estimate — derived from EPA range ×
          kWh/100 mi ÷ usable-fraction.  User can override.

  [v5-4] EPA data shown in results — rated range, city/hwy kWh, MPGe,
          and "% of EPA rating" displayed alongside the computed range.

  [v5-5] Manual fallback — if the EPA API is unreachable, the original
          class-based picker from v4 is used automatically.

Inherited from v4:
  auto weather, auto topo grade, 3-panel plot, regen braking, grade force,
  usable_DoD, Veh_Aux_Pow_W, TripHistory, SOC depletion, US units.

Data sources (all free, no API keys):
  • EPA FuelEconomy.gov  — exact vehicle specs (kWh/100mi, range, MPGe)
  • Nominatim            — geocode place names → lat/lon
  • Open-Meteo           — live weather at origin
  • Open-Topo-Data       — elevation at 12 points along route
  • Haversine            — road distance estimate (straight-line × 1.25)

Dependencies:
  pip install requests matplotlib numpy
"""

from __future__ import annotations
import math, time, sys, xml.etree.ElementTree as ET
import requests
from dataclasses import dataclass, field

try:
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    import numpy as np
    HAS_PLOT = True
except ImportError:
    HAS_PLOT = False

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════
G              = 9.81
SEP            = "─" * 64
SEP2           = "═" * 64
EPA_BASE       = "https://www.fueleconomy.gov/ws/rest"
PRECIP_LABELS  = {"none": "Clear ☀️", "rain": "Rain 🌧️", "snow": "Snow ❄️"}

ROAD_TYPES = {
    "1": {"name": "Highway", "speed_range": (90, 130), "base_speed": 110,
          "cr_mult": 1.00, "regen_factor": 0.05},
    "2": {"name": "Mixed",   "speed_range": (60, 110), "base_speed":  85,
          "cr_mult": 1.10, "regen_factor": 0.18},
    "3": {"name": "City",    "speed_range": (25,  70), "base_speed":  50,
          "cr_mult": 1.25, "regen_factor": 0.35},
}
HVAC_OPTIONS = {"1": "off", "2": "eco", "3": "comfort"}

# ══════════════════════════════════════════════════════════════════════════════
# EPA fueleconomy.gov  API  (XML, no auth needed)
# ══════════════════════════════════════════════════════════════════════════════

def _epa_get(path: str) -> ET.Element:
    """Fetch an EPA REST endpoint and return the root XML element."""
    url = EPA_BASE + path
    r   = requests.get(url, headers={"Accept": "application/xml"}, timeout=12)
    r.raise_for_status()
    return ET.fromstring(r.content)


def _xml_items(root: ET.Element, tag: str = "menuItem") -> list[dict]:
    """Extract list of {value, text} dicts from a menu XML response."""
    items = []
    for el in root.iter(tag):
        value = el.findtext("value") or ""
        text  = el.findtext("text")  or ""
        items.append({"value": value, "text": text})
    return items


def _xml_field(root: ET.Element, field: str) -> str:
    el = root.find(field)
    return (el.text or "").strip() if el is not None else ""


def epa_fetch_years() -> list[dict]:
    root  = _epa_get("/vehicle/menu/year")
    years = _xml_items(root)
    # Newest first; restrict to 2011+ (EVs exist from ~2011)
    return sorted([y for y in years if int(y["value"]) >= 2011],
                  key=lambda y: -int(y["value"]))


def epa_fetch_makes(year: str) -> list[dict]:
    return _xml_items(_epa_get(f"/vehicle/menu/make?year={year}"))


def epa_fetch_models(year: str, make: str) -> list[dict]:
    return _xml_items(_epa_get(
        f"/vehicle/menu/model?year={year}&make={requests.utils.quote(make)}"))


def epa_fetch_options(year: str, make: str, model: str) -> list[dict]:
    """Returns trims; each item's 'value' is the EPA vehicle ID."""
    return _xml_items(_epa_get(
        f"/vehicle/menu/options?year={year}"
        f"&make={requests.utils.quote(make)}"
        f"&model={requests.utils.quote(model)}"))


def epa_fetch_vehicle(vehicle_id: str) -> dict:
    """
    Fetch full record for an EPA vehicle ID.
    Key fields returned:
      make, model, year, drive, atvType, sizeClass
      cityE, hwyE, combE  — kWh per 100 miles (0 if not EV)
      range_mi            — EPA combined range in miles
      comb_mpge           — MPGe combined
      charge_240          — hours to charge at 240 V
    """
    root = _epa_get(f"/vehicle/{vehicle_id}")
    g    = lambda f: _xml_field(root, f)

    city_e  = float(g("cityE")  or 0)
    hwy_e   = float(g("hwyE")   or 0)
    comb_e  = float(g("combE")  or 0)
    range_mi= float(g("range")  or 0)
    mpge    = float(g("comb08") or 0)

    return {
        "epa_id":     vehicle_id,
        "make":       g("make"),
        "model":      g("model"),
        "year":       g("year"),
        "drive":      g("drive"),
        "atv_type":   g("atvType"),       # "EV", "Plug-in Hybrid EV", etc.
        "size_class": g("VClass"),
        "fuel_type":  g("fuelType1"),
        "ev_motor":   g("evMotor"),
        "city_e":     city_e,             # kWh / 100 mi  (city)
        "hwy_e":      hwy_e,              # kWh / 100 mi  (highway)
        "comb_e":     comb_e,             # kWh / 100 mi  (combined)
        "range_mi":   range_mi,           # EPA rated range
        "comb_mpge":  mpge,
        "charge_240": float(g("charge240") or 0),
        # Pre-convert to Wh/km for physics engine
        # kWh/100mi → Wh/km:  × 1000 / 160.934
        "wh_km_city": city_e * 1000 / 160.934 if city_e else 0.0,
        "wh_km_hwy":  hwy_e  * 1000 / 160.934 if hwy_e  else 0.0,
    }


def epa_is_bev(v: dict) -> bool:
    atv = (v.get("atv_type") or "").lower()
    ft  = (v.get("fuel_type") or "").lower()
    return ("ev" in atv and "plug-in hybrid" not in atv) or \
           (ft == "electricity") or (v["city_e"] > 0 and v["hwy_e"] > 0)


def epa_estimate_battery_kwh(v: dict) -> float | None:
    """
    Estimate gross battery capacity from EPA range × efficiency.
    Usable fraction ~88 % assumed; result is gross kWh.
    Returns None if data insufficient.
    """
    if v["comb_e"] > 0 and v["range_mi"] > 0:
        usable_kwh = v["range_mi"] * v["comb_e"] / 100.0
        return round(usable_kwh / 0.88, 1)
    return None


# ── Physics defaults keyed by EPA VClass string ───────────────────────────────
def physics_defaults_from_class(size_class: str) -> dict:
    s = (size_class or "").lower()
    if "pickup" in s:
        return {"mass":3050,"Cd":0.36,"A":3.50,"Cr":0.012,"eta":0.88,"usable_DoD":0.86,"regen_eff":0.28,"Veh_Aux_Pow_W":1800}
    if "van" in s or "minivan" in s:
        return {"mass":3200,"Cd":0.38,"A":4.20,"Cr":0.012,"eta":0.88,"usable_DoD":0.85,"regen_eff":0.28,"Veh_Aux_Pow_W":1800}
    if "special purpose" in s or ("suv" in s and "large" in s):
        return {"mass":2650,"Cd":0.33,"A":3.00,"Cr":0.011,"eta":0.89,"usable_DoD":0.87,"regen_eff":0.30,"Veh_Aux_Pow_W":1600}
    if "suv" in s or "utility" in s or "crossover" in s:
        return {"mass":2050,"Cd":0.29,"A":2.60,"Cr":0.010,"eta":0.90,"usable_DoD":0.88,"regen_eff":0.32,"Veh_Aux_Pow_W":1300}
    if "large" in s:
        return {"mass":1950,"Cd":0.26,"A":2.35,"Cr":0.009,"eta":0.91,"usable_DoD":0.88,"regen_eff":0.34,"Veh_Aux_Pow_W":1200}
    if "midsize" in s or "mid-size" in s:
        return {"mass":1900,"Cd":0.25,"A":2.30,"Cr":0.009,"eta":0.92,"usable_DoD":0.88,"regen_eff":0.35,"Veh_Aux_Pow_W":1200}
    # Compact / subcompact default
    return {"mass":1700,"Cd":0.27,"A":2.20,"Cr":0.009,"eta":0.91,"usable_DoD":0.87,"regen_eff":0.33,"Veh_Aux_Pow_W":1100}


# ══════════════════════════════════════════════════════════════════════════════
# FALLBACK CLASS PICKER  (v4 behaviour — used if EPA API unreachable)
# ══════════════════════════════════════════════════════════════════════════════
VEHICLE_CLASSES = {
    "1": {"class_name":"Compact Sedan / Hatchback","examples":"e.g. Nissan Leaf, VW ID.3, Chevy Bolt",
          "mass":1650,"Cd":0.26,"A":2.15,"Cr":0.009,"eta":0.91,"cap_kwh":None,"usable_DoD":0.87,"regen_eff":0.32,"Veh_Aux_Pow_W":1000},
    "2": {"class_name":"Family Sedan / Fastback","examples":"e.g. Tesla Model 3, IONIQ 6, Polestar 2",
          "mass":1900,"Cd":0.24,"A":2.25,"Cr":0.008,"eta":0.92,"cap_kwh":None,"usable_DoD":0.88,"regen_eff":0.35,"Veh_Aux_Pow_W":1200},
    "3": {"class_name":"Compact SUV / Crossover","examples":"e.g. Tesla Model Y, VW ID.4, IONIQ 5",
          "mass":2050,"Cd":0.29,"A":2.60,"Cr":0.010,"eta":0.90,"cap_kwh":None,"usable_DoD":0.88,"regen_eff":0.32,"Veh_Aux_Pow_W":1300},
    "4": {"class_name":"Large SUV / 7-Seater","examples":"e.g. Tesla Model X, BMW iX, Rivian R1S",
          "mass":2650,"Cd":0.33,"A":3.00,"Cr":0.011,"eta":0.89,"cap_kwh":None,"usable_DoD":0.87,"regen_eff":0.30,"Veh_Aux_Pow_W":1600},
    "5": {"class_name":"Pickup Truck","examples":"e.g. F-150 Lightning, Rivian R1T, Silverado EV",
          "mass":3050,"Cd":0.36,"A":3.50,"Cr":0.012,"eta":0.88,"cap_kwh":None,"usable_DoD":0.86,"regen_eff":0.28,"Veh_Aux_Pow_W":1800},
    "6": {"class_name":"Van / Minivan","examples":"e.g. Ford e-Transit, Mercedes eSprinter",
          "mass":3200,"Cd":0.38,"A":4.20,"Cr":0.012,"eta":0.88,"cap_kwh":None,"usable_DoD":0.85,"regen_eff":0.28,"Veh_Aux_Pow_W":1800},
    "7": {"class_name":"Custom (enter all params)","examples":"Any vehicle",
          "mass":2000,"Cd":0.30,"A":2.50,"Cr":0.010,"eta":0.90,"cap_kwh":None,"usable_DoD":0.87,"regen_eff":0.30,"Veh_Aux_Pow_W":1300},
}
PARAM_META = [
    ("mass","Mass","kg",1000,5500),("Cd","Drag coeff (Cd)","",0.14,0.60),
    ("A","Frontal area","m2",1.4,5.5),("Cr","Rolling resist (Cr)","",0.004,0.022),
    ("eta","Drivetrain efficiency","",0.78,0.97),("cap_kwh","Battery capacity","kWh",15.0,300.0),
    ("usable_DoD","Usable DoD","",0.65,0.97),("regen_eff","Regen efficiency","",0.10,0.55),
    ("Veh_Aux_Pow_W","Base aux load","W",400,3500),
]


# ══════════════════════════════════════════════════════════════════════════════
# DATA CLASS
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class TripHistory:
    soc_pct:     list = field(default_factory=list)
    dist_km:     list = field(default_factory=list)
    speed_kph:   list = field(default_factory=list)
    wh_km:       list = field(default_factory=list)
    segment_lbl: str  = ""

    def record(self, soc_pct, dist_km, speed_kph, wh_km):
        self.soc_pct.append(soc_pct); self.dist_km.append(dist_km)
        self.speed_kph.append(speed_kph); self.wh_km.append(wh_km)

    @property
    def final_soc(self):      return self.soc_pct[-1]  if self.soc_pct  else 0.0
    @property
    def total_distance(self): return self.dist_km[-1]  if self.dist_km  else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# PHYSICS ENGINE  (v5: EPA calibration scalar added)
# ══════════════════════════════════════════════════════════════════════════════
def air_density(t):       return 1.225 * (288.15 / (t + 273.15))
def battery_cap_factor(t):
    if 20 <= t <= 35: return 1.0
    if t < 20:        return max(0.60, 1.0 - (20 - t) * 0.015)
    return max(0.88, 1.0 - (t - 35) * 0.006)
def hvac_watts(t, mode):
    if mode == "off": return 0.0
    d = abs(t - 22)
    return (500 + d*30) if mode == "eco" else (1200 + d*65)
def precip_cr_mult(p):    return {"none":1.0,"rain":1.15,"snow":1.30}.get(p, 1.0)
def soh_from_miles(mi):
    if mi < 20_000: return 1.0
    if mi > 150_000: return 0.75
    return 1.0 - ((mi - 20_000) / 130_000) * 0.25


def _epa_calibration_scalar(vehicle: dict, road: dict) -> float:
    """
    Compute the calibration scalar that aligns the physics engine to the
    EPA kWh/100 mi rating at EPA test conditions:
      • 22 °C  • no wind  • flat (0°)  • no HVAC  • base speed for road type

    scalar = epa_wh_per_km / physics_wh_per_km_at_epa_conditions

    If EPA data is absent or the vehicle dict has no epa_* keys, returns 1.0.
    Clamped to [0.60, 1.40] to prevent runaway corrections.
    """
    wh_city = vehicle.get("epa_wh_km_city", 0.0)
    wh_hwy  = vehicle.get("epa_wh_km_hwy",  0.0)

    if not wh_city and not wh_hwy:
        return 1.0

    # Blend city/hwy based on road type
    if   road["name"] == "Highway": frac = 0.0   # 0 = full highway
    elif road["name"] == "City":    frac = 1.0   # 1 = full city
    else:                           frac = 0.5   # mixed

    epa_wh_km = (1 - frac) * (wh_hwy  or wh_city) + \
                     frac  * (wh_city or wh_hwy)

    # Physics at EPA conditions
    v_ms   = road["base_speed"] / 3.6
    rho    = air_density(22)
    f_rr   = vehicle["Cr"] * road["cr_mult"] * vehicle["mass"] * G
    f_drag = 0.5 * rho * vehicle["Cd"] * vehicle["A"] * v_ms ** 2
    prop_w = ((f_rr + f_drag) * v_ms) / vehicle["eta"]
    regen  = max(0.0, prop_w) * road["regen_factor"] * vehicle["regen_eff"]
    net_w  = max(0.0, prop_w - regen) + vehicle["Veh_Aux_Pow_W"]
    phys_wh_km = (net_w / v_ms) / 3600 * 1000

    if phys_wh_km <= 0:
        return 1.0

    return max(0.60, min(1.40, epa_wh_km / phys_wh_km))


def compute(vehicle, temp_c, wind_kph, precip, hvac, road,
            soc_pct, reserve_pct, miles, dist_km, speed_kph, theta_deg=0.0):
    v_ms    = speed_kph / 3.6
    w_ms    = wind_kph  / 3.6
    theta_r = math.radians(theta_deg)
    rho     = air_density(temp_c)
    cr_m    = precip_cr_mult(precip) * road["cr_mult"]
    eta     = vehicle["eta"]

    f_rr    = vehicle["Cr"] * cr_m * vehicle["mass"] * G
    f_drag  = 0.5 * rho * vehicle["Cd"] * vehicle["A"] * (v_ms + w_ms) ** 2
    f_grade = vehicle["mass"] * G * math.sin(theta_r)
    prop_w  = ((f_rr + f_drag + f_grade) * v_ms) / eta

    base_aux_w     = vehicle["Veh_Aux_Pow_W"]
    hvac_w         = hvac_watts(temp_c, hvac)
    total_w        = prop_w + base_aux_w + hvac_w
    regen_credit_w = max(0.0, prop_w) * road["regen_factor"] * vehicle["regen_eff"]
    net_total_w    = max(0.0, prop_w - regen_credit_w) + base_aux_w + hvac_w
    wh_per_km      = (net_total_w / v_ms) / 3600 * 1000

    # ── EPA calibration ───────────────────────────────────────────────────────
    cal       = _epa_calibration_scalar(vehicle, road)
    wh_per_km *= cal
    # ─────────────────────────────────────────────────────────────────────────

    soh       = soh_from_miles(miles)
    cap_f     = battery_cap_factor(temp_c)
    dod       = vehicle["usable_DoD"]
    usable_f  = max(0.0, (soc_pct - reserve_pct) / 100.0)
    avail_kwh = vehicle["cap_kwh"] * soh * cap_f * dod * usable_f
    range_km  = avail_kwh * 1000 / wh_per_km if wh_per_km > 0 else 0.0

    # Baseline (25 °C, no wind, flat, no HVAC) for % reduction
    b_rho  = air_density(25)
    b_f_rr = vehicle["Cr"] * road["cr_mult"] * vehicle["mass"] * G
    b_drag = 0.5 * b_rho * vehicle["Cd"] * vehicle["A"] * v_ms ** 2
    b_prop = ((b_f_rr + b_drag) * v_ms) / eta
    b_regen= max(0.0, b_prop) * road["regen_factor"] * vehicle["regen_eff"]
    b_net  = max(0.0, b_prop - b_regen) + base_aux_w
    b_wh   = (b_net / v_ms) / 3600 * 1000 * cal
    b_avail= vehicle["cap_kwh"] * 1.0 * 1.0 * dod * usable_f
    b_range= b_avail * 1000 / b_wh if b_wh > 0 else 0.0
    red_pct= max(0.0, (b_range - range_km) / b_range * 100) if b_range > 0 else 0.0

    losses = {
        "aero":           (f_drag  * v_ms / eta) / max(1.0, total_w) * 100,
        "rolling":        (f_rr    * v_ms / eta) / max(1.0, total_w) * 100,
        "grade":          (abs(f_grade)*v_ms/eta) / max(1.0, total_w) * 100,
        "hvac":            hvac_w      / max(1.0, total_w) * 100,
        "base_aux":        base_aux_w  / max(1.0, total_w) * 100,
        "thermal":         (1 - cap_f) * 100,
        "regen_recovery":  regen_credit_w / max(1.0, total_w) * 100,
    }

    return {
        "speed_kph":      speed_kph,
        "range_km":       max(0.0, range_km),
        "range_mi":       max(0.0, range_km * 0.621371),
        "wh_per_km":      wh_per_km,
        "avail_kwh":      avail_kwh,
        "soh":            soh,
        "cap_factor":     cap_f,
        "reduction_pct":  red_pct,
        "can_reach":      range_km >= dist_km if dist_km > 0 else None,
        "losses":         losses,
        "regen_credit_w": regen_credit_w,
        "epa_cal_scalar": cal,
    }


def find_optimal_speed(vehicle, temp_c, wind_kph, precip, hvac,
                       road, soc_pct, reserve_pct, miles, dist_km, theta_deg=0.0):
    lo, hi = road["speed_range"]
    best   = None
    for s in range(lo, hi + 1):
        r = compute(vehicle, temp_c, wind_kph, precip, hvac,
                    road, soc_pct, reserve_pct, miles, dist_km, s, theta_deg)
        if best is None or r["range_km"] > best["range_km"]:
            best = r
    return best


def build_trip_history(vehicle, temp_c, wind_kph, precip, hvac,
                       road, soc_pct, reserve_pct, miles, dist_km,
                       speed_kph, theta_deg=0.0, label="Trip"):
    r        = compute(vehicle, temp_c, wind_kph, precip, hvac,
                       road, soc_pct, reserve_pct, miles, dist_km, speed_kph, theta_deg)
    wh_km    = r["wh_per_km"]
    soh      = soh_from_miles(miles)
    cap_f    = battery_cap_factor(temp_c)
    total_kw = vehicle["cap_kwh"] * soh * cap_f * vehicle["usable_DoD"]
    cur_kwh  = total_kw * (soc_pct / 100.0)
    floor    = total_kw * (reserve_pct / 100.0)
    step     = wh_km / 1000.0

    hist = TripHistory(segment_lbl=label)
    hist.record(cur_kwh / total_kw * 100, 0.0, speed_kph, wh_km)
    for _ in range(int(max(dist_km, r["range_km"])) + 2):
        cur_kwh -= step
        d = hist.dist_km[-1] + 1.0
        hist.record(max(0.0, cur_kwh / total_kw * 100), d, speed_kph, wh_km)
        if cur_kwh <= floor:
            break
    return hist


# ══════════════════════════════════════════════════════════════════════════════
# GEO + WEATHER + ELEVATION
# ══════════════════════════════════════════════════════════════════════════════
def geocode(place: str) -> dict:
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": place, "format": "json", "limit": 1},
        headers={"User-Agent": "ev-range-v5/1.0", "Accept-Language": "en"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f'Cannot find "{place}"')
    d = data[0]
    return {"lat": float(d["lat"]), "lon": float(d["lon"]),
            "name": d["display_name"].split(",")[0]}


def fetch_weather(lat: float, lon: float) -> dict:
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": lat, "longitude": lon,
                "current": "temperature_2m,wind_speed_10m,precipitation,weather_code",
                "wind_speed_unit": "kmh", "timezone": "auto"},
        timeout=10,
    )
    r.raise_for_status()
    c  = r.json()["current"]
    wc = c["weather_code"]
    precip = "none"
    if 51 <= wc <= 67:  precip = "rain"
    elif 71 <= wc <= 77: precip = "snow"
    if c["precipitation"] > 0.5 and precip == "none": precip = "rain"
    return {"temp_c": round(c["temperature_2m"], 1),
            "wind_kph": round(c["wind_speed_10m"]), "precip": precip}


def haversine_km(a: dict, b: dict) -> float:
    R    = 6371.0
    dLat = math.radians(b["lat"] - a["lat"])
    dLon = math.radians(b["lon"] - a["lon"])
    x    = (math.sin(dLat/2)**2 +
            math.cos(math.radians(a["lat"])) *
            math.cos(math.radians(b["lat"])) *
            math.sin(dLon/2)**2)
    return R * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))


def fetch_elevation_profile(orig: dict, dest: dict, n: int = 12) -> dict:
    points  = [{"lat": orig["lat"] + i/(n-1)*(dest["lat"]-orig["lat"]),
                "lon": orig["lon"] + i/(n-1)*(dest["lon"]-orig["lon"])}
               for i in range(n)]
    latlons = "|".join(f"{p['lat']:.5f},{p['lon']:.5f}" for p in points)
    try:
        r     = requests.get("https://api.open-topo-data.com/v1/srtm90m",
                             params={"locations": latlons}, timeout=12)
        r.raise_for_status()
        elevs = [res.get("elevation") or 0.0 for res in r.json().get("results", [])]
    except Exception:
        elevs = [0.0] * n
    total_km = haversine_km(orig, dest) * 1.25
    dist_pts = [i/(n-1)*total_km for i in range(n)]
    gain = sum(max(0, elevs[i+1]-elevs[i]) for i in range(n-1))
    loss = sum(max(0, elevs[i]-elevs[i+1]) for i in range(n-1))
    net  = elevs[-1] - elevs[0]
    deg  = math.degrees(math.atan2(net, total_km * 1000))
    return {"elevations_m": elevs, "dist_km_pts": dist_pts,
            "mean_grade_deg": deg, "total_gain_m": gain, "total_loss_m": loss,
            "flat_fallback": all(e == 0.0 for e in elevs)}


# ══════════════════════════════════════════════════════════════════════════════
# PLOTS  (unchanged from v4)
# ══════════════════════════════════════════════════════════════════════════════
def plot_results(vehicle, wx, road, soc_pct, reserve_pct, miles,
                 dist_km, theta_deg, hist_base, hist_opt, optimal,
                 elev_profile, orig_name, dest_name,
                 epa_data=None, save_path="ev_range_analysis.png"):
    if not HAS_PLOT:
        print("  Install matplotlib+numpy: pip install matplotlib numpy")
        return

    fig = plt.figure(figsize=(17, 6))
    epa_tag = f"  |  EPA {epa_data['range_mi']:.0f} mi rated" if epa_data and epa_data.get("range_mi") else ""
    fig.suptitle(
        f"{vehicle['name']}  |  {orig_name} → {dest_name}"
        f"  |  {road['name']}  |  {wx['temp_c']}°C  {wx['precip'].upper()}{epa_tag}",
        fontsize=11, fontweight="bold",
    )

    gs  = fig.add_gridspec(1, 3, wspace=0.38)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])
    ax3 = fig.add_subplot(gs[2])
    colors = ["#2563EB", "#10B981"]

    # Panel 1 — SOC depletion
    for hist, col in zip([hist_base, hist_opt], colors):
        ax1.plot([d*0.621 for d in hist.dist_km], hist.soc_pct,
                 lw=2.2, color=col, label=hist.segment_lbl)
    ax1.axhline(reserve_pct, color="#EF4444", ls="--", lw=1.3,
                label=f"Reserve {reserve_pct}%")
    if dist_km > 0:
        ax1.axvline(dist_km*0.621, color="#94A3B8", ls=":", lw=1.3,
                    label=f"Dest. {dist_km*0.621:.0f} mi")
    ax1.set_xlabel("Distance [mi]"); ax1.set_ylabel("State of Charge [%]")
    ax1.set_title("SOC Depletion", fontweight="bold"); ax1.set_ylim(0, 105)
    ax1.legend(fontsize=8); ax1.grid(True, alpha=0.22)

    # Panel 2 — Range vs Speed
    lo, hi     = road["speed_range"]
    speeds_kph = list(range(lo, hi+1, 5))
    speeds     = [round(s*0.621) for s in speeds_kph]
    results_   = [compute(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                          "eco", road, soc_pct, reserve_pct, miles, dist_km,
                          s, theta_deg) for s in speeds_kph]
    ranges     = [r["range_mi"]        for r in results_]
    wh_vals    = [r["wh_per_km"]*1.609 for r in results_]

    from matplotlib.collections import LineCollection
    from matplotlib.colors       import Normalize
    from matplotlib.cm           import ScalarMappable
    speeds_a = np.array(speeds, dtype=float)
    ranges_a = np.array(ranges, dtype=float)
    wh_a     = np.array(wh_vals, dtype=float)
    pts      = np.array([speeds_a, ranges_a]).T.reshape(-1, 1, 2)
    segs     = np.concatenate([pts[:-1], pts[1:]], axis=1)
    norm     = Normalize(vmin=wh_a.min(), vmax=wh_a.max())
    cmap     = plt.cm.RdYlGn_r
    lc       = LineCollection(segs, cmap=cmap, norm=norm, linewidth=2.8)
    lc.set_array((wh_a[:-1]+wh_a[1:])/2); ax2.add_collection(lc)

    for spd, rng, wh in zip(speeds, ranges, wh_vals):
        if spd % 10 == 0:
            ax2.plot(spd, rng, "o", ms=4, color=cmap(norm(wh)), zorder=5)
            ax2.annotate(f"{rng:.0f} mi", (spd, rng),
                         xytext=(3,5), textcoords="offset points",
                         fontsize=7, color="#374151")

    opt_spd = round(optimal["speed_kph"]*0.621)
    ax2.axvline(opt_spd, color="#10B981", ls="--", lw=1.4,
                label=f"Optimal {opt_spd} mph")
    ax2.plot(opt_spd, optimal["range_mi"], "^", ms=9, color="#10B981", zorder=6)

    if dist_km > 0:
        ax2.axhline(dist_km*0.621, color="#94A3B8", ls=":", lw=1.2,
                    label=f"Route {dist_km*0.621:.0f} mi")
    # EPA rated range line
    if epa_data and epa_data.get("range_mi"):
        ax2.axhline(epa_data["range_mi"], color="#059669", ls="-.", lw=1.2,
                    label=f"EPA rated {epa_data['range_mi']:.0f} mi")

    plt.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=ax2,
                 label="Wh/mi", shrink=0.75, pad=0.02)
    ax2.set_xlim(round(lo*0.621)-2, round(hi*0.621)+2)
    ax2.set_ylim(0, max(ranges)*1.12)
    ax2.set_xlabel("Cruise Speed [mph]"); ax2.set_ylabel("Estimated Range [mi]")
    ax2.set_title("Range vs Speed", fontweight="bold")
    ax2.legend(fontsize=8); ax2.grid(True, alpha=0.2)

    # Panel 3 — Elevation profile
    e_pts   = elev_profile["elevations_m"]
    d_pts   = [x*0.621 for x in elev_profile["dist_km_pts"]]
    is_flat = elev_profile["flat_fallback"]
    if is_flat:
        ax3.text(0.5, 0.5, "Elevation data\nunavailable\n(flat assumed)",
                 ha="center", va="center", transform=ax3.transAxes,
                 fontsize=10, color="#94A3B8")
    else:
        ax3.fill_between(d_pts, e_pts, alpha=0.25, color="#D97706")
        ax3.plot(d_pts, e_pts, lw=2, color="#D97706")
        ax3.plot(d_pts[0],  e_pts[0],  "o", ms=7, color="#2563EB", label=orig_name[:14])
        ax3.plot(d_pts[-1], e_pts[-1], "s", ms=7, color="#10B981", label=dest_name[:14])
        gain = elev_profile["total_gain_m"]*3.281; loss = elev_profile["total_loss_m"]*3.281
        ax3.set_title(f"Elevation  (grade {elev_profile['mean_grade_deg']:+.2f}°)\n"
                      f"↑ {gain:.0f} ft  ·  ↓ {loss:.0f} ft",
                      fontsize=10, fontweight="bold")
        ax3.legend(fontsize=8)
        ax3.yaxis.set_major_formatter(mticker.FormatStrFormatter("%d m"))
    ax3.set_xlabel("Distance [mi]"); ax3.set_ylabel("Elevation [m]")
    ax3.grid(True, alpha=0.22)

    plt.subplots_adjust(left=0.06, right=0.96, top=0.88, bottom=0.12, wspace=0.44)
    try:
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"\n  📊  Plot saved → {save_path}")
    except Exception as e:
        print(f"\n  (Plot save failed: {e})")
    try:
        plt.show()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# CLI HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def bar(pct, width=28):
    filled = round(min(100.0, pct) / 100 * width)
    return "█" * filled + "░" * (width - filled)

def pick(prompt, options, default="1"):
    for k, v in options.items():
        print(f"    [{k}] {v if isinstance(v, str) else v['name']}")
    c = input(f"  {prompt} [{default}]: ").strip() or default
    while c not in options:
        c = input(f"  Invalid. Choose {list(options.keys())}: ").strip() or default
    return c

def ask_int(prompt, default, lo, hi):
    while True:
        raw = input(f"  {prompt} [{default}]: ").strip() or str(default)
        try:
            v = int(raw)
            if lo <= v <= hi: return v
        except ValueError: pass
        print(f"    Enter {lo}–{hi}.")

def _ask_float_bounded(prompt, lo, hi, default=None):
    while True:
        raw = input(prompt).strip()
        if raw == "" and default is not None: return float(default)
        try:
            v = float(raw)
            if lo <= v <= hi: return v
            print(f"    Enter {lo}–{hi}.")
        except ValueError:
            print("    Enter a valid number.")

def _numbered_menu(items: list[dict], key="text") -> dict:
    """Print a numbered list and return the chosen item."""
    for i, item in enumerate(items, 1):
        print(f"    [{i:>2}] {item[key]}")
    while True:
        raw = input(f"  Choice [1]: ").strip() or "1"
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(items): return items[idx]
        except ValueError: pass
        print(f"    Enter 1–{len(items)}.")


# ══════════════════════════════════════════════════════════════════════════════
# VEHICLE PICKER — EPA first, v4 class fallback
# ══════════════════════════════════════════════════════════════════════════════

def pick_vehicle_epa(road: dict) -> tuple[dict, dict | None]:
    """
    Interactive EPA vehicle lookup:
      Year → Make → Model → Trim

    Returns (vehicle_dict, epa_data_dict).
    vehicle_dict is ready for compute(); epa_data_dict holds EPA metadata
    for display.  Raises on network/parse errors.
    """
    print("\n  Connecting to EPA FuelEconomy.gov…")

    # ── Year ──────────────────────────────────────────────────────────────────
    years = epa_fetch_years()
    print(f"\n  Model Year:  ({len(years)} available, newest first)")
    yr_item = _numbered_menu(years[:15])          # show up to 15 years
    year    = yr_item["value"]
    print(f"  → {year}")
    time.sleep(0.4)

    # ── Make ──────────────────────────────────────────────────────────────────
    print(f"\n  Fetching makes for {year}…")
    makes   = epa_fetch_makes(year)
    print(f"  Make:  ({len(makes)} available)")
    mk_item = _numbered_menu(makes)
    make    = mk_item["value"]
    print(f"  → {make}")
    time.sleep(0.4)

    # ── Model ─────────────────────────────────────────────────────────────────
    print(f"\n  Fetching models for {year} {make}…")
    models  = epa_fetch_models(year, make)
    print(f"  Model:  ({len(models)} available)")
    md_item = _numbered_menu(models)
    model   = md_item["value"]
    print(f"  → {model}")
    time.sleep(0.4)

    # ── Trim ──────────────────────────────────────────────────────────────────
    print(f"\n  Fetching trims for {year} {make} {model}…")
    options = epa_fetch_options(year, make, model)
    print(f"  Trim / configuration:  ({len(options)} available)")
    tr_item = _numbered_menu(options)
    epa_id  = tr_item["value"]
    print(f"  → {tr_item['text']}")
    time.sleep(0.4)

    # ── Fetch full record ──────────────────────────────────────────────────────
    print(f"\n  Fetching EPA data for vehicle ID {epa_id}…")
    epa = epa_fetch_vehicle(epa_id)

    is_ev = epa_is_bev(epa)
    if is_ev:
        print(f"  ✓ BEV confirmed  ·  {epa['city_e']} / {epa['hwy_e']} kWh/100mi  "
              f"·  EPA range {epa['range_mi']:.0f} mi  ·  {epa['comb_mpge']:.0f} MPGe")
    else:
        print(f"  ⚠  Not a pure BEV (atvType={epa['atv_type']!r}).  "
              f"Some EPA fields may be zero.  Proceeding with best available data.")

    # ── Build vehicle dict ────────────────────────────────────────────────────
    phys = physics_defaults_from_class(epa["size_class"])

    # Battery capacity
    est_kwh = epa_estimate_battery_kwh(epa)
    if est_kwh:
        print(f"\n  Estimated battery capacity: {est_kwh} kWh  "
              f"(from EPA range × kWh/100mi ÷ 0.88)")
        raw = input(f"  Accept {est_kwh} kWh? [Enter=yes, or type override]: ").strip()
        cap_kwh = float(raw) if raw else est_kwh
    else:
        cap_kwh = _ask_float_bounded(
            "  Battery capacity (kWh) [15–300]: ", 15.0, 300.0)

    # Allow physics override
    print()
    print(f"  Physics defaults (from EPA size class: {epa['size_class'] or 'unknown'}):")
    print(f"    mass {phys['mass']} kg · Cd {phys['Cd']} · A {phys['A']} m² · "
          f"Cr {phys['Cr']} · η {phys['eta']} · DoD {phys['usable_DoD']} · "
          f"regen {phys['regen_eff']} · aux {phys['Veh_Aux_Pow_W']} W")
    override = input("  Override any physics param? [y/N]: ").strip().lower()

    vehicle = {
        "name":          f"{year} {make} {model}",
        "cap_kwh":       cap_kwh,
        # Physics
        "mass":          phys["mass"],
        "Cd":            phys["Cd"],
        "A":             phys["A"],
        "Cr":            phys["Cr"],
        "eta":           phys["eta"],
        "usable_DoD":    phys["usable_DoD"],
        "regen_eff":     phys["regen_eff"],
        "Veh_Aux_Pow_W": phys["Veh_Aux_Pow_W"],
        # EPA calibration anchors
        "epa_wh_km_city": epa["wh_km_city"],
        "epa_wh_km_hwy":  epa["wh_km_hwy"],
        "epa_range_mi":   epa["range_mi"],
        "epa_mpge":       epa["comb_mpge"],
    }

    if override == "y":
        print("  Enter new value or press Enter to keep default.\n")
        for key, label, unit, lo, hi in PARAM_META:
            if key == "cap_kwh": continue   # already set
            unit_str = f" {unit}" if unit else ""
            current  = vehicle.get(key, phys.get(key))
            raw = input(f"    {label} [{current}{unit_str}]: ").strip()
            if raw:
                vehicle[key] = _ask_float_bounded(f"    Re-enter {label} ({lo}–{hi}): ",
                                                   lo, hi, default=current)

    print(f"\n  ✓ Vehicle: {vehicle['name']}")
    print(f"    {vehicle['cap_kwh']} kWh  ·  Cd {vehicle['Cd']}  ·  "
          f"{vehicle['mass']:.0f} kg  ·  regen {vehicle['regen_eff']*100:.0f}%")
    if epa["wh_km_hwy"] > 0:
        scalar = _epa_calibration_scalar(vehicle, road)
        print(f"    EPA calibration scalar: {scalar:.3f}  "
              f"(physics scaled to match {epa['hwy_e']} kWh/100mi EPA highway)")

    return vehicle, epa


def pick_vehicle_fallback() -> dict:
    """Original v4 class-based picker — used when EPA API is unreachable."""
    print("\n  (Using class-based fallback picker)\n")
    print("  Vehicle class:")
    for k, cls in VEHICLE_CLASSES.items():
        tag = "(enter all params)" if k == "7" else ""
        print(f"    [{k}] {cls['class_name']}  {tag}")
        print(f"        {cls['examples']}")
    print()
    choice   = input("  Select class [1]: ").strip() or "1"
    while choice not in VEHICLE_CLASSES:
        choice = input(f"  Invalid. Enter 1–{len(VEHICLE_CLASSES)}: ").strip() or "1"
    cls       = VEHICLE_CLASSES[choice]
    is_custom = choice == "7"
    vehicle   = {"name": cls["class_name"]}
    for key, label, unit, lo, hi in PARAM_META:
        default  = cls[key]
        unit_str = f" {unit}" if unit else ""
        if key == "cap_kwh":
            typical = {"1":"30–65","2":"60–100","3":"58–84","4":"80–135","5":"100–180","6":"60–120"}.get(choice,"40–150")
            vehicle[key] = _ask_float_bounded(f"  {label} (kWh) [typical {typical} kWh]: ", lo, hi)
        elif is_custom:
            vehicle[key] = _ask_float_bounded(f"  {label}{unit_str} [{lo}–{hi}] [{default}]: ", lo, hi, default=default)
        else:
            raw = input(f"  {label}: {default}{unit_str}  (Enter to keep): ").strip()
            vehicle[key] = default if not raw else _ask_float_bounded(f"    Re-enter: ", lo, hi, default=default)
    return vehicle


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    print()
    print(SEP2)
    print("  ⚡  EV RANGE MODELLER v5  —  EPA-Calibrated Physics")
    print(SEP2)
    print()

    # ── STEP 1: ROUTE ─────────────────────────────────────────────────────────
    print("STEP 1 / 3  —  Route\n" + SEP)

    origin_str = input("  Origin city        : ").strip() or "New York, NY"
    dest_str   = input("  Destination city   : ").strip() or "Boston, MA"

    print("\n  Road type:")
    road_key = pick("Select", ROAD_TYPES)
    road     = ROAD_TYPES[road_key]

    print(f"\n  Locating '{origin_str}'…")
    orig_geo = geocode(origin_str);  time.sleep(1.1)
    print(f"  Locating '{dest_str}'…")
    dest_geo = geocode(dest_str);    time.sleep(1.1)

    straight_km = haversine_km(orig_geo, dest_geo)
    dist_km     = round(straight_km * 1.25)
    print(f"  ✓ {orig_geo['name']}  →  {dest_geo['name']}"
          f"  ({round(dist_km*0.621)} mi / {dist_km} km)")

    print(f"\n  Fetching live weather at {orig_geo['name']}…")
    wx = fetch_weather(orig_geo["lat"], orig_geo["lon"])
    print(f"  ✓ {wx['temp_c']}°C  ·  {round(wx['wind_kph']*0.621)} mph wind"
          f"  ·  {PRECIP_LABELS[wx['precip']]}")

    print(f"\n  Fetching elevation profile…")
    elev      = fetch_elevation_profile(orig_geo, dest_geo, n=12)
    theta_deg = elev["mean_grade_deg"]
    if elev["flat_fallback"]:
        print("  ✓ Elevation unavailable — assumed flat (0.00°)")
    else:
        print(f"  ✓ Grade {theta_deg:+.2f}°  ·  "
              f"↑ {round(elev['total_gain_m']*3.281)} ft  ·  "
              f"↓ {round(elev['total_loss_m']*3.281)} ft")

    # ── STEP 2: VEHICLE & CONDITIONS ──────────────────────────────────────────
    print("\nSTEP 2 / 3  —  Vehicle & Conditions\n" + SEP)

    epa_data = None
    try:
        vehicle, epa_data = pick_vehicle_epa(road)
    except Exception as e:
        print(f"\n  ⚠  EPA API unavailable ({e})")
        print("  Falling back to class-based vehicle picker…")
        vehicle = pick_vehicle_fallback()

    soc_pct     = ask_int("Battery level (%)", 85, 5, 100)
    reserve_pct = ask_int("Reserve SOC  (%)", 10, 0, 30)
    miles       = float(input("  Odometer (miles) [30000]: ").strip() or "30000")

    print("  Climate control:")
    hvac = HVAC_OPTIONS[pick("Select", {"1":"off","2":"eco","3":"comfort"})]

    # ── STEP 3: COMPUTE ───────────────────────────────────────────────────────
    print("\nSTEP 3 / 3  —  Computing…\n" + SEP)

    base_result = compute(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                          hvac, road, soc_pct, reserve_pct, miles, dist_km,
                          road["base_speed"], theta_deg)
    optimal     = find_optimal_speed(vehicle, wx["temp_c"], wx["wind_kph"],
                                     wx["precip"], hvac, road, soc_pct,
                                     reserve_pct, miles, dist_km, theta_deg)

    hist_base = build_trip_history(vehicle, wx["temp_c"], wx["wind_kph"],
                                   wx["precip"], hvac, road, soc_pct,
                                   reserve_pct, miles, dist_km,
                                   road["base_speed"], theta_deg,
                                   label=f"Base {round(road['base_speed']*0.621)} mph")
    hist_opt  = build_trip_history(vehicle, wx["temp_c"], wx["wind_kph"],
                                   wx["precip"], hvac, road, soc_pct,
                                   reserve_pct, miles, dist_km,
                                   optimal["speed_kph"], theta_deg,
                                   label=f"Optimal {round(optimal['speed_kph']*0.621)} mph")

    # ── OUTPUT ────────────────────────────────────────────────────────────────
    print(); print(SEP2); print("  RESULTS"); print(SEP2)

    r = base_result
    o = optimal

    # Compute arrival SOC at destination for both base and optimal speeds
    total_usable    = vehicle["cap_kwh"] * r["soh"] * r["cap_factor"] * vehicle["usable_DoD"]
    energy_used     = dist_km * r["wh_per_km"] / 1000
    energy_used_o   = dist_km * o["wh_per_km"] / 1000
    start_kwh       = total_usable * (soc_pct / 100.0)
    arrival_kwh     = max(0.0, start_kwh - energy_used)
    arrival_kwh_o   = max(0.0, start_kwh - energy_used_o)
    arrival_soc     = arrival_kwh   / total_usable * 100 if total_usable > 0 else 0.0
    arrival_soc_o   = arrival_kwh_o / total_usable * 100 if total_usable > 0 else 0.0
    above_reserve   = arrival_soc   - reserve_pct
    above_reserve_o = arrival_soc_o - reserve_pct

    print(f"\n  Route   :  {orig_geo['name']}  →  {dest_geo['name']}")
    print(f"  Vehicle :  {vehicle['name']}")
    print(f"  Road    :  {road['name']}  |  {round(dist_km*0.621)} mi  ({dist_km} km)")
    print(f"  Weather :  {wx['temp_c']}°C  ·  {round(wx['wind_kph']*0.621)} mph"
          f"  ·  {PRECIP_LABELS[wx['precip']]}")
    if not elev["flat_fallback"]:
        print(f"  Grade   :  {theta_deg:+.2f}°  "
              f"(↑{round(elev['total_gain_m']*3.281)} ft / ↓{round(elev['total_loss_m']*3.281)} ft)")

    # EPA data block
    if epa_data:
        print(f"\n  EPA DATA  ({epa_data['year']} {epa_data['make']} {epa_data['model']})")
        if epa_data["range_mi"] > 0:
            print(f"    Rated range       : {epa_data['range_mi']:.0f} mi")
        if epa_data["city_e"] > 0:
            print(f"    City efficiency   : {epa_data['city_e']} kWh/100 mi")
        if epa_data["hwy_e"] > 0:
            print(f"    Highway efficiency: {epa_data['hwy_e']} kWh/100 mi")
        if epa_data["comb_mpge"] > 0:
            print(f"    Combined MPGe     : {epa_data['comb_mpge']:.0f}")
        if epa_data["charge_240"] > 0:
            print(f"    240V charge time  : {epa_data['charge_240']:.1f} hr")
        cal = base_result["epa_cal_scalar"]
        print(f"    Calibration scalar: {cal:.3f}  "
              f"({'physics ↑ to match EPA' if cal > 1 else 'physics ↓ to match EPA'})")

    print(f"\n  Battery :  {soc_pct}% SOC  ·  DoD {vehicle['usable_DoD']*100:.0f}%"
          f"  ·  SOH {r['soh']*100:.1f}%  ·  HVAC {hvac}")
    print(); print(SEP)

    # Range box
    print(f"\n  ┌─ ESTIMATED RANGE AT {round(road['base_speed']*0.621)} mph {'─'*32}┐")
    print(f"  │   {str(round(r['range_mi'])) + ' mi':>7}  ({round(r['range_km'])} km)"
          + " " * 35 + "│")
    if epa_data and epa_data["range_mi"] > 0:
        pct_epa = r["range_mi"] / epa_data["range_mi"] * 100
        print(f"  │   vs EPA rated       : {pct_epa:.0f}% of {epa_data['range_mi']:.0f} mi EPA"
              + " " * 14 + "│")
    print(f"  │   Arrival SOC        : {arrival_soc:.1f}%  "
          f"({'above' if above_reserve >= 0 else 'BELOW'} {reserve_pct}% reserve "
          f"by {abs(above_reserve):.1f}%)" + " " * 4 + "│")
    print(f"  │   Available energy   : {r['avail_kwh']:.2f} kWh" + " " * 28 + "│")
    print(f"  │   Consumption        : {r['wh_per_km']*1.609:.1f} Wh/mi  ({r['wh_per_km']:.1f} Wh/km)"
          + " " * 7 + "│")
    print(f"  │   Regen recovery     : {r['regen_credit_w']:.0f} W"
          f"  ({r['losses']['regen_recovery']:.1f}%)" + " " * 22 + "│")
    if r["reduction_pct"] > 0.5:
        print(f"  │   vs ideal (25°C)    : ↓ {r['reduction_pct']:.1f}% range reduction"
              + " " * 20 + "│")
    print(f"  └{'─'*62}┘")

    # Verdict
    print()
    if r["can_reach"] is True:
        print(f"  ✅  YOU'LL MAKE IT")
        print(f"      Arriving at {arrival_soc:.1f}% SOC  "
              f"(+{above_reserve:.1f}% above your {reserve_pct}% reserve)")
    elif r["can_reach"] is False:
        if optimal["can_reach"]:
            print(f"  ⚠️   At {round(road['base_speed']*0.621)} mph you'd arrive at {arrival_soc:.1f}% SOC — "
                  f"{abs(above_reserve):.1f}% BELOW your {reserve_pct}% reserve")
            print(f"      → Drive at {round(o['speed_kph']*0.621)} mph: arrive at "
                  f"{arrival_soc_o:.1f}% SOC (+{above_reserve_o:.1f}% above reserve)")
        else:
            short = round((dist_km - o["range_km"]) * 0.621)
            print(f"  ❌  CHARGE STOP NEEDED  —  battery runs out {short} mi short "
                  f"even at optimal {round(o['speed_kph']*0.621)} mph")

    # Optimal speed
    o = optimal
    print(); print(SEP)
    print(f"\n  OPTIMAL SPEED  (maximises range under these exact conditions)\n")
    print(f"    Speed       :  {round(o['speed_kph']*0.621)} mph  ({o['speed_kph']} km/h)")
    print(f"    Range       :  {round(o['range_mi'])} mi  ({round(o['range_km'])} km)")
    print(f"    Consumption :  {o['wh_per_km']*1.609:.1f} Wh/mi  ({o['wh_per_km']:.1f} Wh/km)")
    gain = round((o["range_km"] - r["range_km"]) * 0.621)
    if gain > 0:
        print(f"    vs base speed: +{gain} mi range gain")

    # ASCII speed curve
    lo, hi   = road["speed_range"]
    spd_list = list(range(lo, hi+1, 10))
    rng_list = [compute(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                        hvac, road, soc_pct, reserve_pct, miles, dist_km,
                        s, theta_deg)["range_mi"] for s in spd_list]
    max_r    = max(rng_list)
    print(f"\n  Range vs Speed  ({round(lo*0.621)}–{round(hi*0.621)} mph, every ~6 mph)\n")
    HEIGHT = 7
    for row in range(HEIGHT, 0, -1):
        line = "  "
        for spd, rng in zip(spd_list, rng_list):
            if rng >= max_r * row / HEIGHT:
                line += "▲" if spd == o["speed_kph"] else "█"
            else:
                line += "▲" if (spd == o["speed_kph"] and row == 1) else " "
        if row == HEIGHT: line += f"  {round(max_r)} mi"
        print(line)
    print("  " + "‾" * len(spd_list))
    axis = ("  " + str(round(lo*0.621)) +
            " " * (len(spd_list) - len(str(round(lo*0.621))) - len(str(round(hi*0.621)))) +
            str(round(hi*0.621)) + " mph")
    print(axis)
    print(f"  ▲ = optimal ({round(o['speed_kph']*0.621)} mph)")

    # Loss breakdown
    print(); print(SEP); print("\n  ENERGY LOSS BREAKDOWN\n")
    for lbl, key in [("Aerodynamic drag  ","aero"),("Rolling resistance","rolling"),
                     ("Grade resistance  ","grade"),("HVAC              ","hvac"),
                     ("Base aux (12V+)   ","base_aux"),("Thermal derating  ","thermal")]:
        pct = r["losses"][key]
        if pct > 0.1:
            print(f"  {lbl}  {bar(pct)} {pct:5.1f}%")
    rp = r["losses"]["regen_recovery"]
    print(f"\n  Regen recovery    {bar(rp)} +{rp:4.1f}%  ← returned")

    # Conditions snapshot
    print(); print(SEP); print("\n  CONDITIONS SNAPSHOT\n")
    for k, v in [
        ("Temperature",      f"{wx['temp_c']} °C"),
        ("Wind",             f"{round(wx['wind_kph']*0.621)} mph"),
        ("Precipitation",    PRECIP_LABELS[wx["precip"]]),
        ("Mean grade",       f"{theta_deg:+.2f}°" + (" (SRTM)" if not elev["flat_fallback"] else " (flat fallback)")),
        ("Elev gain/loss",   f"↑{elev['total_gain_m']:.0f} m / ↓{elev['total_loss_m']:.0f} m"),
        ("Battery health",   f"{r['soh']*100:.1f}%"),
        ("Arrival SOC",      f"{arrival_soc:.1f}%  ({'+' if above_reserve >= 0 else ''}{above_reserve:.1f}% vs {reserve_pct}% reserve)"),
        ("Capacity factor",  f"{r['cap_factor']*100:.1f}%"),
        ("Available energy", f"{r['avail_kwh']:.2f} kWh"),
        ("Drivetrain η",     f"{vehicle['eta']*100:.0f}%"),
        ("Regen efficiency", f"{vehicle['regen_eff']*100:.0f}%"),
        ("Base aux",         f"{vehicle['Veh_Aux_Pow_W']} W"),
        ("EPA cal. scalar",  f"{r['epa_cal_scalar']:.3f}" if epa_data else "N/A (class defaults)"),
    ]:
        print(f"  {k:<22}  {v}")

    # Plot
    plot_results(
        vehicle=vehicle, wx=wx, road=road,
        soc_pct=soc_pct, reserve_pct=reserve_pct,
        miles=miles, dist_km=dist_km, theta_deg=theta_deg,
        hist_base=hist_base, hist_opt=hist_opt,
        optimal=optimal, elev_profile=elev,
        epa_data=epa_data,
        orig_name=orig_geo["name"], dest_name=dest_geo["name"],
        save_path="ev_range_analysis_v5.png",
    )

    print(); print(SEP2); print("  Done. ⚡"); print(SEP2); print()


if __name__ == "__main__":
    main()
