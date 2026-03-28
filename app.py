import os
import io
import re
import time
from math import radians, cos, sin, asin, sqrt
from html import escape

from flask import Flask, request
import pandas as pd
import requests

try:
    import pdfplumber
    PDFPLUMBER_OK = True
except Exception:
    pdfplumber = None
    PDFPLUMBER_OK = False

app = Flask(__name__)

# =========================================
# CONFIG
# =========================================
STOP_DISTANCE_METERS = 15
STOP_MINUTES = 3
BASE_RADIUS_METERS = 100
MATCH_DISTANCE_METERS = 20
GEOCODE_TIMEOUT = 12

GEOCODE_CACHE = {}

# =========================================
# UTILS
# =========================================
def haversine(lat1, lon1, lat2, lon2):
    if pd.isna(lat1) or pd.isna(lon1) or pd.isna(lat2) or pd.isna(lon2):
        return None
    r = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(a ** 0.5)


def fmt_dt(x):
    if pd.isna(x):
        return "-"
    return pd.to_datetime(x).strftime("%Y-%m-%d %H:%M:%S")


def fmt_minutes(m):
    if m is None or pd.isna(m):
        return "-"
    m = int(round(float(m)))
    h = m // 60
    mm = m % 60
    return f"{h} h {mm} min"


def normalize_text(s):
    if s is None or pd.isna(s):
        return ""
    return str(s).strip()


def find_col(df, candidates):
    cols = {str(c).strip().lower(): c for c in df.columns}
    for cand in candidates:
        cand = cand.lower()
        for c_norm, c_real in cols.items():
            if cand == c_norm or cand in c_norm:
