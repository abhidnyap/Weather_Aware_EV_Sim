"""
EV Range Simulator — Streamlit UI
Powered by ev_range_v5 physics engine
"""

import streamlit as st
import math
import requests
import xml.etree.ElementTree as ET
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="EV Range Simulator",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  [data-testid="stSidebar"] { background: #f8fafc; }
  .metric-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 16px 18px;
    text-align: center;
  }
  .metric-card .label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: #94a3b8;
    margin-bottom: 4px;
  }
  .metric-card .value {
    font-size: 22px;
    font-weight: 700;
    color: #0f172a;
  }
  .metric-card .sub {
    font-size: 11px;
    color: #94a3b8;
    margin-top: 2px;
  }
  .verdict-ok   { background:#f0fdf4; border:1px solid #86efac; border-radius:8px; padding:16px; }
  .verdict-warn { background:#fffbeb; border:1px solid #fcd34d; border-radius:8px; padding:16px; }
  .verdict-bad  { background:#fff1f2; border:1px solid #fca5a5; border-radius:8px; padding:16px; }
  .epa-badge {
    display:inline-block; background:#eff6ff; border:1px solid #bfdbfe;
    color:#1d4ed8; border-radius:4px; padding:3px 8px;
    font-size:11px; margin:2px;
  }
  h1 { color: #0f172a !important; }
  .section-head {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.12em;
    color: #d97706; font-weight: 700; margin-bottom: 6px; margin-top: 18px;
  }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════
G = 9.81
EPA_BASE = "https://www.fueleconomy.gov/ws/rest"

ROAD_TYPES = {
    "Highway": {"name":"Highway","speed_range":(90,130),"base_speed":110,"cr_mult":1.00,"regen_factor":0.05},
    "Mixed":   {"name":"Mixed",  "speed_range":(60,110),"base_speed": 85,"cr_mult":1.10,"regen_factor":0.18},
    "City":    {"name":"City",   "speed_range":(25, 70),"base_speed": 50,"cr_mult":1.25,"regen_factor":0.35},
}
PRECIP_LABELS = {"none":"Clear ☀️","rain":"Rain 🌧️","snow":"Snow ❄️"}

# ══════════════════════════════════════════════════════════════════════════════
# EPA API  (cached so dropdowns don't re-fetch on every widget interaction)
# ══════════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=86400, show_spinner=False)
def epa_fetch_years():
    root  = ET.fromstring(requests.get(f"{EPA_BASE}/vehicle/menu/year",
                          headers={"Accept":"application/xml"}, timeout=12).content)
    items = [{"value":el.findtext("value"),"text":el.findtext("text")}
             for el in root.iter("menuItem")]
    return sorted([i for i in items if int(i["value"]) >= 2011],
                  key=lambda x: -int(x["value"]))

@st.cache_data(ttl=86400, show_spinner=False)
def epa_fetch_makes(year):
    root = ET.fromstring(requests.get(f"{EPA_BASE}/vehicle/menu/make?year={year}",
                         headers={"Accept":"application/xml"}, timeout=12).content)
    return [{"value":el.findtext("value"),"text":el.findtext("text")}
            for el in root.iter("menuItem")]

@st.cache_data(ttl=86400, show_spinner=False)
def epa_fetch_models(year, make):
    url  = f"{EPA_BASE}/vehicle/menu/model?year={year}&make={requests.utils.quote(make)}"
    root = ET.fromstring(requests.get(url, headers={"Accept":"application/xml"}, timeout=12).content)
    return [{"value":el.findtext("value"),"text":el.findtext("text")}
            for el in root.iter("menuItem")]

@st.cache_data(ttl=86400, show_spinner=False)
def epa_fetch_options(year, make, model):
    url  = (f"{EPA_BASE}/vehicle/menu/options?year={year}"
            f"&make={requests.utils.quote(make)}&model={requests.utils.quote(model)}")
    root = ET.fromstring(requests.get(url, headers={"Accept":"application/xml"}, timeout=12).content)
    return [{"value":el.findtext("value"),"text":el.findtext("text")}
            for el in root.iter("menuItem")]

@st.cache_data(ttl=86400, show_spinner=False)
def epa_fetch_vehicle(vid):
    root = ET.fromstring(requests.get(f"{EPA_BASE}/vehicle/{vid}",
                         headers={"Accept":"application/xml"}, timeout=12).content)
    g = lambda f: (root.find(f).text or "").strip() if root.find(f) is not None else ""
    city_e = float(g("cityE") or 0)
    hwy_e  = float(g("hwyE")  or 0)
    comb_e = float(g("combE") or 0)
    range_ = float(g("range") or 0)
    return {
        "make":g("make"),"model":g("model"),"year":g("year"),
        "drive":g("drive"),"atv_type":g("atvType"),"size_class":g("VClass"),
        "city_e":city_e,"hwy_e":hwy_e,"comb_e":comb_e,
        "range_mi":range_,"comb_mpge":float(g("comb08") or 0),
        "charge_240":float(g("charge240") or 0),
        "wh_km_city": city_e*1000/160.934 if city_e else 0.0,
        "wh_km_hwy":  hwy_e *1000/160.934 if hwy_e  else 0.0,
    }

# ══════════════════════════════════════════════════════════════════════════════
# PHYSICS  (identical to ev_range_v5.py)
# ══════════════════════════════════════════════════════════════════════════════
def air_density(t):       return 1.225*(288.15/(t+273.15))
def battery_cap_factor(t):
    if 20<=t<=35: return 1.0
    if t<20:      return max(0.60, 1.0-(20-t)*0.015)
    return max(0.88, 1.0-(t-35)*0.006)
def hvac_watts(t, mode):
    if mode=="off": return 0.0
    d = abs(t-22)
    return (500+d*30) if mode=="eco" else (1200+d*65)
def precip_cr_mult(p):    return {"none":1.0,"rain":1.15,"snow":1.30}.get(p,1.0)
def soh_from_miles(mi):
    if mi<20000:  return 1.0
    if mi>150000: return 0.75
    return 1.0-((mi-20000)/130000)*0.25

def physics_defaults_from_class(size_class):
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
    return {"mass":1700,"Cd":0.27,"A":2.20,"Cr":0.009,"eta":0.91,"usable_DoD":0.87,"regen_eff":0.33,"Veh_Aux_Pow_W":1100}

def epa_calibration_scalar(vehicle, road):
    wh_city = vehicle.get("epa_wh_km_city",0.0)
    wh_hwy  = vehicle.get("epa_wh_km_hwy", 0.0)
    if not wh_city and not wh_hwy: return 1.0
    frac = 0.0 if road["name"]=="Highway" else 1.0 if road["name"]=="City" else 0.5
    epa_wh = (1-frac)*(wh_hwy or wh_city)+frac*(wh_city or wh_hwy)
    v_ms   = road["base_speed"]/3.6
    rho    = air_density(22)
    f_rr   = vehicle["Cr"]*road["cr_mult"]*vehicle["mass"]*G
    f_drag = 0.5*rho*vehicle["Cd"]*vehicle["A"]*v_ms**2
    prop_w = ((f_rr+f_drag)*v_ms)/vehicle["eta"]
    regen  = max(0.0,prop_w)*road["regen_factor"]*vehicle["regen_eff"]
    net_w  = max(0.0,prop_w-regen)+vehicle["Veh_Aux_Pow_W"]
    phys   = (net_w/v_ms)/3600*1000
    return max(0.60,min(1.40,epa_wh/phys)) if phys>0 else 1.0

def compute(vehicle, temp_c, wind_kph, precip, hvac, road,
            soc_pct, reserve_pct, miles, dist_km, speed_kph, theta_deg=0.0):
    v_ms    = speed_kph/3.6
    w_ms    = wind_kph/3.6
    theta_r = math.radians(theta_deg)
    rho     = air_density(temp_c)
    cr_m    = precip_cr_mult(precip)*road["cr_mult"]
    eta     = vehicle["eta"]
    f_rr    = vehicle["Cr"]*cr_m*vehicle["mass"]*G
    f_drag  = 0.5*rho*vehicle["Cd"]*vehicle["A"]*(v_ms+w_ms)**2
    f_grade = vehicle["mass"]*G*math.sin(theta_r)
    prop_w  = ((f_rr+f_drag+f_grade)*v_ms)/eta
    base_aux_w     = vehicle["Veh_Aux_Pow_W"]
    hvac_w         = hvac_watts(temp_c, hvac)
    total_w        = prop_w+base_aux_w+hvac_w
    regen_credit_w = max(0.0,prop_w)*road["regen_factor"]*vehicle["regen_eff"]
    net_total_w    = max(0.0,prop_w-regen_credit_w)+base_aux_w+hvac_w
    wh_per_km      = (net_total_w/v_ms)/3600*1000
    cal       = epa_calibration_scalar(vehicle, road)
    wh_per_km *= cal
    soh       = soh_from_miles(miles)
    cap_f     = battery_cap_factor(temp_c)
    dod       = vehicle["usable_DoD"]
    usable_f  = max(0.0,(soc_pct-reserve_pct)/100.0)
    avail_kwh = vehicle["cap_kwh"]*soh*cap_f*dod*usable_f
    range_km  = avail_kwh*1000/wh_per_km if wh_per_km>0 else 0.0
    b_rho=air_density(25); b_fRR=vehicle["Cr"]*road["cr_mult"]*vehicle["mass"]*G
    b_drag=0.5*b_rho*vehicle["Cd"]*vehicle["A"]*v_ms**2
    b_prop=((b_fRR+b_drag)*v_ms)/eta
    b_regen=max(0.0,b_prop)*road["regen_factor"]*vehicle["regen_eff"]
    b_net=max(0.0,b_prop-b_regen)+base_aux_w
    b_wh=(b_net/v_ms)/3600*1000*cal
    b_av=vehicle["cap_kwh"]*1.0*1.0*dod*usable_f
    b_rng=b_av*1000/b_wh if b_wh>0 else 0.0
    red_pct=max(0.0,(b_rng-range_km)/b_rng*100) if b_rng>0 else 0.0
    safeW=max(1.0,total_w)
    losses={
        "aero":   (f_drag*v_ms/eta)/safeW*100,
        "rolling":(f_rr*v_ms/eta)/safeW*100,
        "grade":  (abs(f_grade)*v_ms/eta)/safeW*100,
        "hvac":    hvac_w/safeW*100,
        "base_aux":base_aux_w/safeW*100,
        "thermal": (1-cap_f)*100,
        "regen_recovery":regen_credit_w/safeW*100,
    }
    return {
        "speed_kph":speed_kph,"range_km":max(0.0,range_km),
        "range_mi":max(0.0,range_km*0.621371),"wh_per_km":wh_per_km,
        "avail_kwh":avail_kwh,"soh":soh,"cap_factor":cap_f,
        "reduction_pct":red_pct,"can_reach":range_km>=dist_km if dist_km>0 else None,
        "losses":losses,"regen_credit_w":regen_credit_w,"epa_cal_scalar":cal,
    }

def find_optimal_speed(vehicle, temp_c, wind_kph, precip, hvac,
                       road, soc_pct, reserve_pct, miles, dist_km, theta_deg=0.0):
    lo, hi = road["speed_range"]
    best   = None
    for s in range(lo, hi+1):
        r = compute(vehicle, temp_c, wind_kph, precip, hvac,
                    road, soc_pct, reserve_pct, miles, dist_km, s, theta_deg)
        if best is None or r["range_km"] > best["range_km"]:
            best = {**r, "speed_kph": s}
    return best

def build_soc_trace(vehicle, temp_c, wind_kph, precip, hvac,
                    road, soc_pct, reserve_pct, miles, dist_km, speed_kph, theta_deg):
    r       = compute(vehicle, temp_c, wind_kph, precip, hvac,
                      road, soc_pct, reserve_pct, miles, dist_km, speed_kph, theta_deg)
    soh     = soh_from_miles(miles)
    cap_f   = battery_cap_factor(temp_c)
    total   = vehicle["cap_kwh"]*soh*cap_f*vehicle["usable_DoD"]
    cur     = total*(soc_pct/100.0)
    floor   = total*(reserve_pct/100.0)
    step    = r["wh_per_km"]/1000.0
    dist_mi, soc_vals = [0.0], [cur/total*100]
    for _ in range(int(max(dist_km, r["range_km"]))+2):
        cur -= step
        dist_mi.append(dist_mi[-1]+0.621)
        soc_vals.append(max(0.0, cur/total*100))
        if cur <= floor: break
    return dist_mi, soc_vals

# ══════════════════════════════════════════════════════════════════════════════
# GEO / WEATHER / ELEVATION
# ══════════════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=300, show_spinner=False)
def geocode(place):
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q":place,"format":"json","limit":1},
                     headers={"User-Agent":"ev-range-streamlit/1.0","Accept-Language":"en"},
                     timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data: raise ValueError(f'Cannot find "{place}"')
    d = data[0]
    return {"lat":float(d["lat"]),"lon":float(d["lon"]),"name":d["display_name"].split(",")[0]}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_weather(lat, lon):
    r = requests.get("https://api.open-meteo.com/v1/forecast",
                     params={"latitude":lat,"longitude":lon,
                             "current":"temperature_2m,wind_speed_10m,precipitation,weather_code",
                             "wind_speed_unit":"kmh","timezone":"auto"},
                     timeout=10)
    r.raise_for_status()
    c  = r.json()["current"]
    wc = c["weather_code"]
    precip = "none"
    if 51<=wc<=67:   precip = "rain"
    elif 71<=wc<=77: precip = "snow"
    if c["precipitation"]>0.5 and precip=="none": precip = "rain"
    return {"temp_c":round(c["temperature_2m"],1),
            "wind_kph":round(c["wind_speed_10m"]),"precip":precip}

def haversine_km(a, b):
    R=6371.0; dLat=math.radians(b["lat"]-a["lat"]); dLon=math.radians(b["lon"]-a["lon"])
    x=(math.sin(dLat/2)**2+math.cos(math.radians(a["lat"]))*math.cos(math.radians(b["lat"]))*math.sin(dLon/2)**2)
    return R*2*math.atan2(math.sqrt(x),math.sqrt(1-x))

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_elevation(orig_lat, orig_lon, dest_lat, dest_lon, n=12):
    orig={"lat":orig_lat,"lon":orig_lon}; dest={"lat":dest_lat,"lon":dest_lon}
    pts=[{"lat":orig["lat"]+i/(n-1)*(dest["lat"]-orig["lat"]),
          "lon":orig["lon"]+i/(n-1)*(dest["lon"]-orig["lon"])} for i in range(n)]
    latlons="|".join(f"{p['lat']:.5f},{p['lon']:.5f}" for p in pts)
    try:
        r=requests.get("https://api.open-topo-data.com/v1/srtm90m",
                       params={"locations":latlons},timeout=12)
        r.raise_for_status()
        elevs=[res.get("elevation") or 0.0 for res in r.json().get("results",[])]
    except Exception:
        elevs=[0.0]*n
    total_km=haversine_km(orig,dest)*1.25
    dist_pts=[i/(n-1)*total_km for i in range(n)]
    gain=sum(max(0,elevs[i+1]-elevs[i]) for i in range(n-1))
    loss=sum(max(0,elevs[i]-elevs[i+1]) for i in range(n-1))
    net=elevs[-1]-elevs[0]
    deg=math.degrees(math.atan2(net,total_km*1000))
    return {"elevations_m":elevs,"dist_km_pts":dist_pts,"mean_grade_deg":deg,
            "total_gain_m":gain,"total_loss_m":loss,
            "flat_fallback":all(e==0.0 for e in elevs)}

# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown("## ⚡ EV Range Simulator")
    st.caption("EPA Data · Live Weather · Physics-Based")
    st.divider()

    # ── 01 Route ──────────────────────────────────────────────────────────────
    st.markdown('<div class="section-head">01 · Route</div>', unsafe_allow_html=True)
    origin_str = st.text_input("Origin",      value="New York, NY",  placeholder="City, State")
    dest_str   = st.text_input("Destination", value="Boston, MA",    placeholder="City, State")
    road_name  = st.selectbox("Road type", list(ROAD_TYPES.keys()))
    road       = ROAD_TYPES[road_name]

    # ── 02 Vehicle — EPA lookup ───────────────────────────────────────────────
    st.markdown('<div class="section-head">02 · Vehicle — EPA Lookup</div>', unsafe_allow_html=True)
    st.caption("Live data from fueleconomy.gov · Every BEV sold in the US from 2011")

    epa_data = None
    vehicle  = None

    try:
        years      = epa_fetch_years()
        year_opts  = [y["value"] for y in years]
        year_sel   = st.selectbox("Model year", year_opts, index=0)

        makes      = epa_fetch_makes(year_sel)
        make_opts  = [m["text"] for m in makes]
        make_sel   = st.selectbox("Make", make_opts)

        models     = epa_fetch_models(year_sel, make_sel)
        model_opts = [m["text"] for m in models]
        model_sel  = st.selectbox("Model", model_opts)

        trims      = epa_fetch_options(year_sel, make_sel, model_sel)
        trim_opts  = [t["text"] for t in trims]
        trim_sel   = st.selectbox("Trim / configuration", trim_opts)
        trim_id    = trims[trim_opts.index(trim_sel)]["value"]

        epa_data   = epa_fetch_vehicle(trim_id)
        phys       = physics_defaults_from_class(epa_data["size_class"])

        # Battery capacity
        est_kwh = None
        if epa_data["comb_e"] > 0 and epa_data["range_mi"] > 0:
            est_kwh = round(epa_data["range_mi"] * epa_data["comb_e"] / 100.0 / 0.88, 1)

        cap_kwh = st.number_input(
            "Battery capacity (kWh)",
            min_value=10.0, max_value=300.0, step=0.5,
            value=float(est_kwh) if est_kwh else 75.0,
            help=f"Auto-estimated from EPA data: {est_kwh} kWh" if est_kwh else "Enter manually"
        )

        vehicle = {
            "name":          f"{year_sel} {make_sel} {model_sel}",
            "cap_kwh":       cap_kwh,
            "mass":          phys["mass"],
            "Cd":            phys["Cd"],
            "A":             phys["A"],
            "Cr":            phys["Cr"],
            "eta":           phys["eta"],
            "usable_DoD":    phys["usable_DoD"],
            "regen_eff":     phys["regen_eff"],
            "Veh_Aux_Pow_W": phys["Veh_Aux_Pow_W"],
            "epa_wh_km_city": epa_data["wh_km_city"],
            "epa_wh_km_hwy":  epa_data["wh_km_hwy"],
        }

    except Exception as e:
        st.warning(f"EPA API unavailable — using manual entry ({e})")
        cap_kwh = st.number_input("Battery capacity (kWh)", 10.0, 300.0, 75.0, 0.5)
        vehicle = {
            "name":"Custom EV","cap_kwh":cap_kwh,"mass":2000,"Cd":0.30,"A":2.50,
            "Cr":0.010,"eta":0.90,"usable_DoD":0.87,"regen_eff":0.30,"Veh_Aux_Pow_W":1300,
            "epa_wh_km_city":0.0,"epa_wh_km_hwy":0.0,
        }

    # Advanced physics overrides
    with st.expander("⚙ Advanced physics params"):
        vehicle["mass"]          = st.number_input("Mass (kg)",           1000, 5500,  int(vehicle["mass"]))
        vehicle["Cd"]            = st.number_input("Drag coeff (Cd)",     0.14, 0.60,  vehicle["Cd"],  0.01)
        vehicle["A"]             = st.number_input("Frontal area (m²)",   1.4,  5.5,   vehicle["A"],   0.1)
        vehicle["Cr"]            = st.number_input("Rolling resist (Cr)", 0.004,0.022, vehicle["Cr"],  0.001)
        vehicle["eta"]           = st.number_input("Drivetrain eff",      0.78, 0.97,  vehicle["eta"], 0.01)
        vehicle["usable_DoD"]    = st.number_input("Usable DoD",          0.65, 0.97,  vehicle["usable_DoD"], 0.01)
        vehicle["regen_eff"]     = st.number_input("Regen efficiency",    0.10, 0.55,  vehicle["regen_eff"],  0.01)
        vehicle["Veh_Aux_Pow_W"] = st.number_input("Base aux load (W)",   400,  3500,  int(vehicle["Veh_Aux_Pow_W"]))

    # ── 03 Conditions ─────────────────────────────────────────────────────────
    st.markdown('<div class="section-head">03 · Conditions</div>', unsafe_allow_html=True)
    soc_pct     = st.slider("Battery level (%)",  5,  100, 85)
    reserve_pct = st.slider("Reserve SOC (%)",    0,   30, 10)
    miles       = st.number_input("Odometer (miles)", 0, 300000, 30000, 1000)
    hvac        = st.selectbox("Climate control", ["off", "eco", "comfort"])

    compute_btn = st.button("⚡ COMPUTE RANGE", use_container_width=True, type="primary")

# ══════════════════════════════════════════════════════════════════════════════
# MAIN PANEL
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("# ⚡ EV Range Simulator")
st.caption("Physics-based range prediction · EPA-calibrated · Live weather & elevation")

if not compute_btn:
    st.info("Configure your route and vehicle in the sidebar, then click **⚡ COMPUTE RANGE**.")
    st.stop()

# ── Fetch geo / weather / elevation ──────────────────────────────────────────
with st.spinner("Geocoding route…"):
    try:
        orig_geo = geocode(origin_str)
        dest_geo = geocode(dest_str)
    except Exception as e:
        st.error(f"Geocoding failed: {e}"); st.stop()

dist_km = round(haversine_km(orig_geo, dest_geo) * 1.25)

with st.spinner("Fetching live weather…"):
    try:
        wx = fetch_weather(orig_geo["lat"], orig_geo["lon"])
    except Exception as e:
        st.warning(f"Weather fetch failed ({e}) — using 20°C, calm, clear")
        wx = {"temp_c":20.0,"wind_kph":0,"precip":"none"}

with st.spinner("Fetching elevation profile…"):
    elev = fetch_elevation(orig_geo["lat"], orig_geo["lon"],
                           dest_geo["lat"], dest_geo["lon"])

theta_deg = elev["mean_grade_deg"]

# ── Run physics ───────────────────────────────────────────────────────────────
with st.spinner("Running physics engine…"):
    r = compute(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                hvac, road, soc_pct, reserve_pct, miles, dist_km,
                road["base_speed"], theta_deg)
    o = find_optimal_speed(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                           hvac, road, soc_pct, reserve_pct, miles, dist_km, theta_deg)

# ── Arrival SOC ───────────────────────────────────────────────────────────────
total_usable    = vehicle["cap_kwh"]*r["soh"]*r["cap_factor"]*vehicle["usable_DoD"]
energy_used     = dist_km*r["wh_per_km"]/1000
energy_used_o   = dist_km*o["wh_per_km"]/1000
start_kwh       = total_usable*(soc_pct/100.0)
arrival_soc     = max(0.0,(start_kwh-energy_used)/total_usable*100)  if total_usable>0 else 0.0
arrival_soc_o   = max(0.0,(start_kwh-energy_used_o)/total_usable*100) if total_usable>0 else 0.0
above_reserve   = arrival_soc   - reserve_pct
above_reserve_o = arrival_soc_o - reserve_pct
can_reach       = r["can_reach"]
opt_reach       = o["can_reach"]

# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════

# ── Route header ──────────────────────────────────────────────────────────────
st.markdown(f"### {orig_geo['name']} → {dest_geo['name']}")
weather_row = (f"**{round(dist_km*0.621)} mi** · "
               f"{wx['temp_c']}°C · {round(wx['wind_kph']*0.621)} mph wind · "
               f"{PRECIP_LABELS[wx['precip']]}")
if not elev["flat_fallback"]:
    weather_row += f" · Grade {theta_deg:+.2f}°"
st.caption(weather_row)

# ── EPA badges ────────────────────────────────────────────────────────────────
if epa_data and (epa_data["city_e"] > 0 or epa_data["hwy_e"] > 0):
    badges = ""
    if epa_data["city_e"] > 0:   badges += f'<span class="epa-badge">City {epa_data["city_e"]} kWh/100mi</span>'
    if epa_data["hwy_e"]  > 0:   badges += f'<span class="epa-badge">Hwy {epa_data["hwy_e"]} kWh/100mi</span>'
    if epa_data["comb_mpge"] > 0:badges += f'<span class="epa-badge">{epa_data["comb_mpge"]:.0f} MPGe</span>'
    if epa_data["range_mi"] > 0: badges += f'<span class="epa-badge">Rated {epa_data["range_mi"]:.0f} mi</span>'
    badges += f'<span class="epa-badge" style="background:#f0fdf4;border-color:#86efac;color:#166534">⚙ EPA calibrated · scalar {r["epa_cal_scalar"]:.3f}</span>'
    st.markdown(badges, unsafe_allow_html=True)
    st.write("")

# ── Verdict ───────────────────────────────────────────────────────────────────
if can_reach:
    verdict_class = "verdict-ok"
    verdict_icon  = "✅"
    verdict_text  = (f"**You'll make it** — arriving at **{arrival_soc:.1f}% SOC** "
                     f"(+{above_reserve:.1f}% above your {reserve_pct}% reserve)")
elif opt_reach:
    verdict_class = "verdict-warn"
    verdict_icon  = "⚠️"
    verdict_text  = (f"At {round(road['base_speed']*0.621)} mph you'd arrive at "
                     f"**{arrival_soc:.1f}% SOC** — {abs(above_reserve):.1f}% below reserve.  "
                     f"Drive **{round(o['speed_kph']*0.621)} mph** to arrive at "
                     f"**{arrival_soc_o:.1f}% SOC** (+{above_reserve_o:.1f}% above reserve)")
else:
    verdict_class = "verdict-bad"
    verdict_icon  = "❌"
    short_mi      = round((dist_km - o["range_km"]) * 0.621)
    verdict_text  = (f"**Charge stop needed** — battery runs out "
                     f"**{short_mi} mi short** even at optimal {round(o['speed_kph']*0.621)} mph")

st.markdown(
    f'<div class="{verdict_class}">{verdict_icon} {verdict_text}</div>',
    unsafe_allow_html=True
)
st.write("")

# ── Key metrics ───────────────────────────────────────────────────────────────
col1, col2, col3, col4, col5, col6 = st.columns(6)

gain_mi = round((o["range_mi"] - r["range_mi"]))

def metric_card(col, label, value, sub="", red=False):
    color = "#b91c1c" if red else "#0f172a"
    sub_color = "#fca5a5" if red else "#94a3b8"
    border = "1px solid #fca5a5" if red else "1px solid #e2e8f0"
    bg = "rgba(220,38,38,0.04)" if red else "#ffffff"
    col.markdown(f"""
    <div class="metric-card" style="border:{border};background:{bg}">
      <div class="label">{label}</div>
      <div class="value" style="color:{color}">{value}</div>
      <div class="sub" style="color:{sub_color}">{sub}</div>
    </div>""", unsafe_allow_html=True)

metric_card(col1, "Optimal speed",    f"{round(o['speed_kph']*0.621)} mph",
            f"{o['speed_kph']} km/h")
metric_card(col2, "Range @ optimal",  f"{round(o['range_mi'])} mi",
            f"+{gain_mi} mi vs {round(road['base_speed']*0.621)} mph" if gain_mi>0 else "")
metric_card(col3, "Arrival SOC",       f"{arrival_soc:.1f}%",
            f"reserve {reserve_pct}%", red=(arrival_soc < reserve_pct))
metric_card(col4, "Consumption",       f"{r['wh_per_km']*1.609:.1f} Wh/mi",
            f"{r['wh_per_km']:.1f} Wh/km")
metric_card(col5, "Available energy",  f"{r['avail_kwh']:.1f} kWh",
            f"SOH {r['soh']*100:.0f}%")
metric_card(col6, "Temp derating",     f"{r['reduction_pct']:.1f}%",
            "vs 25°C baseline")

st.write("")

# ── EPA data card ─────────────────────────────────────────────────────────────
if epa_data and epa_data["range_mi"] > 0:
    pct_epa = r["range_mi"] / epa_data["range_mi"] * 100
    with st.expander(f"📋 EPA Data — {vehicle['name']}", expanded=True):
        ec1, ec2, ec3, ec4 = st.columns(4)
        ec1.metric("EPA Rated Range",    f"{epa_data['range_mi']:.0f} mi")
        ec2.metric("City efficiency",    f"{epa_data['city_e']} kWh/100mi")
        ec3.metric("Highway efficiency", f"{epa_data['hwy_e']} kWh/100mi")
        ec4.metric("Combined MPGe",      f"{epa_data['comb_mpge']:.0f}")
        if epa_data["charge_240"] > 0:
            st.caption(f"240V charge time: {epa_data['charge_240']:.1f} hr · "
                       f"Drive: {epa_data['drive']} · "
                       f"Your predicted range is **{pct_epa:.0f}% of EPA rated**")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# CHARTS
# ══════════════════════════════════════════════════════════════════════════════
tab1, tab2, tab3, tab4 = st.tabs(["📈 Range vs Speed", "🔋 SOC Depletion", "⛰ Elevation", "📊 Loss Breakdown"])

# ── Tab 1: Range vs Speed ─────────────────────────────────────────────────────
with tab1:
    lo, hi     = road["speed_range"]
    speeds_kph = list(range(lo, hi+1, 5))
    speeds_mph = [round(s*0.621) for s in speeds_kph]
    results_   = [compute(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                          hvac, road, soc_pct, reserve_pct, miles, dist_km,
                          s, theta_deg) for s in speeds_kph]
    ranges_mi  = [r_["range_mi"] for r_ in results_]
    wh_vals    = [r_["wh_per_km"]*1.609 for r_ in results_]

    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(
        x=speeds_mph, y=ranges_mi, mode="lines+markers",
        line=dict(color="#d97706", width=3),
        marker=dict(size=6, color=wh_vals, colorscale="RdYlGn_r",
                    showscale=True, colorbar=dict(title="Wh/mi", thickness=12)),
        name="Range",
        hovertemplate="<b>%{x} mph</b><br>Range: %{y:.0f} mi<extra></extra>"
    ))
    fig1.add_hline(y=dist_km*0.621, line_dash="dot", line_color="#94a3b8",
                   annotation_text=f"Route {round(dist_km*0.621)} mi")
    fig1.add_vline(x=round(o["speed_kph"]*0.621), line_dash="dash", line_color="#10b981",
                   annotation_text=f"Optimal {round(o['speed_kph']*0.621)} mph")
    if epa_data and epa_data["range_mi"] > 0:
        fig1.add_hline(y=epa_data["range_mi"], line_dash="dashdot", line_color="#059669",
                       annotation_text=f"EPA rated {epa_data['range_mi']:.0f} mi")
    fig1.update_layout(xaxis_title="Cruise Speed (mph)", yaxis_title="Estimated Range (mi)",
                       height=380, margin=dict(t=20,b=40), plot_bgcolor="#ffffff",
                       paper_bgcolor="#ffffff", font=dict(size=12))
    fig1.update_xaxes(gridcolor="#f1f5f9"); fig1.update_yaxes(gridcolor="#f1f5f9")
    st.plotly_chart(fig1, use_container_width=True)

# ── Tab 2: SOC Depletion ──────────────────────────────────────────────────────
with tab2:
    d_base, soc_base = build_soc_trace(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                                        hvac, road, soc_pct, reserve_pct, miles, dist_km,
                                        road["base_speed"], theta_deg)
    d_opt,  soc_opt  = build_soc_trace(vehicle, wx["temp_c"], wx["wind_kph"], wx["precip"],
                                        hvac, road, soc_pct, reserve_pct, miles, dist_km,
                                        o["speed_kph"], theta_deg)
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=d_base, y=soc_base, mode="lines", name=f"Base {round(road['base_speed']*0.621)} mph",
                              line=dict(color="#2563eb", width=2.5)))
    fig2.add_trace(go.Scatter(x=d_opt,  y=soc_opt,  mode="lines", name=f"Optimal {round(o['speed_kph']*0.621)} mph",
                              line=dict(color="#10b981", width=2.5)))
    fig2.add_hline(y=reserve_pct, line_dash="dash", line_color="#ef4444",
                   annotation_text=f"Reserve {reserve_pct}%")
    if dist_km > 0:
        fig2.add_vline(x=dist_km*0.621, line_dash="dot", line_color="#94a3b8",
                       annotation_text=f"Dest. {round(dist_km*0.621)} mi")
    fig2.add_annotation(x=dist_km*0.621, y=arrival_soc,
                        text=f"Arrival: {arrival_soc:.1f}%",
                        showarrow=True, arrowhead=2, arrowcolor="#2563eb",
                        bgcolor="white", bordercolor="#2563eb")
    fig2.update_layout(xaxis_title="Distance (mi)", yaxis_title="State of Charge (%)",
                       yaxis=dict(range=[0,105]), height=380, margin=dict(t=20,b=40),
                       plot_bgcolor="#ffffff", paper_bgcolor="#ffffff", font=dict(size=12))
    fig2.update_xaxes(gridcolor="#f1f5f9"); fig2.update_yaxes(gridcolor="#f1f5f9")
    st.plotly_chart(fig2, use_container_width=True)

# ── Tab 3: Elevation ──────────────────────────────────────────────────────────
with tab3:
    if elev["flat_fallback"]:
        st.info("Elevation data unavailable — route assumed flat.")
    else:
        d_pts  = [x*0.621 for x in elev["dist_km_pts"]]
        e_pts  = elev["elevations_m"]
        fig3   = go.Figure()
        fig3.add_trace(go.Scatter(x=d_pts, y=e_pts, mode="lines", fill="tozeroy",
                                  fillcolor="rgba(217,119,6,0.15)",
                                  line=dict(color="#d97706", width=2.5), name="Elevation"))
        fig3.add_trace(go.Scatter(x=[d_pts[0]],  y=[e_pts[0]],  mode="markers",
                                  marker=dict(size=10, color="#2563eb"), name=orig_geo["name"][:20]))
        fig3.add_trace(go.Scatter(x=[d_pts[-1]], y=[e_pts[-1]], mode="markers",
                                  marker=dict(size=10, color="#10b981"), name=dest_geo["name"][:20]))
        fig3.update_layout(
            title=f"Grade {theta_deg:+.2f}° · ↑{elev['total_gain_m']*3.281:.0f} ft · ↓{elev['total_loss_m']*3.281:.0f} ft",
            xaxis_title="Distance (mi)", yaxis_title="Elevation (m)",
            height=380, margin=dict(t=50,b=40),
            plot_bgcolor="#ffffff", paper_bgcolor="#ffffff", font=dict(size=12))
        fig3.update_xaxes(gridcolor="#f1f5f9"); fig3.update_yaxes(gridcolor="#f1f5f9")
        st.plotly_chart(fig3, use_container_width=True)

# ── Tab 4: Loss Breakdown ─────────────────────────────────────────────────────
with tab4:
    loss_labels = ["Aero drag","Rolling resist","Grade","HVAC","Base aux","Thermal derating"]
    loss_keys   = ["aero","rolling","grade","hvac","base_aux","thermal"]
    loss_vals   = [r["losses"][k] for k in loss_keys]
    regen_val   = r["losses"]["regen_recovery"]
    colors_loss = ["#3b82f6","#f59e0b","#8b5cf6","#ef4444","#64748b","#94a3b8"]

    fig4 = go.Figure()
    fig4.add_trace(go.Bar(x=loss_labels, y=loss_vals, marker_color=colors_loss,
                          name="Loss %", hovertemplate="%{x}: %{y:.1f}%<extra></extra>"))
    fig4.add_trace(go.Bar(x=["Regen recovery"], y=[regen_val],
                          marker_color="#10b981", name="Regen (returned)",
                          hovertemplate="Regen recovery: %{y:.1f}%<extra></extra>"))
    fig4.update_layout(xaxis_title="", yaxis_title="% of total power",
                       height=380, margin=dict(t=20,b=40),
                       plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
                       font=dict(size=12), showlegend=True)
    fig4.update_xaxes(gridcolor="#f1f5f9"); fig4.update_yaxes(gridcolor="#f1f5f9")
    st.plotly_chart(fig4, use_container_width=True)

    lc1, lc2, lc3 = st.columns(3)
    lc1.metric("Aero drag",        f"{r['losses']['aero']:.1f}%")
    lc2.metric("Regen recovery",   f"+{regen_val:.1f}%")
    lc3.metric("Thermal derating", f"{r['losses']['thermal']:.1f}%")

# ── Conditions snapshot ───────────────────────────────────────────────────────
st.divider()
with st.expander("🔍 Full conditions snapshot"):
    sc1, sc2, sc3 = st.columns(3)
    sc1.markdown(f"""
**Route**
- Distance: {round(dist_km*0.621)} mi ({dist_km} km)
- Origin: {orig_geo['name']}
- Destination: {dest_geo['name']}
- Road type: {road['name']}
""")
    sc2.markdown(f"""
**Weather & terrain**
- Temperature: {wx['temp_c']}°C
- Wind: {round(wx['wind_kph']*0.621)} mph
- Precipitation: {PRECIP_LABELS[wx['precip']]}
- Mean grade: {theta_deg:+.2f}°
- Elev gain/loss: ↑{elev['total_gain_m']:.0f} m / ↓{elev['total_loss_m']:.0f} m
""")
    sc3.markdown(f"""
**Battery & drivetrain**
- Start SOC: {soc_pct}% · Reserve: {reserve_pct}%
- Arrival SOC: {arrival_soc:.1f}% ({'+' if above_reserve>=0 else ''}{above_reserve:.1f}% vs reserve)
- SOH: {r['soh']*100:.1f}% · Cap factor: {r['cap_factor']*100:.1f}%
- Available energy: {r['avail_kwh']:.2f} kWh
- Drivetrain η: {vehicle['eta']*100:.0f}%
- EPA cal. scalar: {r['epa_cal_scalar']:.3f}
""")
