def link_google_maps(lat, lon):
    if pd.isna(lat) or pd.isna(lon):
        return ""
    return f"https://www.google.com/maps?q={lat},{lon}"

def zona_aproximada_desde_coordenadas(lat, lon):
    if pd.isna(lat) or pd.isna(lon):
        return "Zona no disponible"

    # Clasificación muy básica orientativa; después la podemos mejorar
    # o reemplazar por geocodificación real.
    if -34.75 <= lat <= -34.30 and -58.90 <= lon <= -58.20:
        return "AMBA / Buenos Aires aproximado"
    return f"Zona aproximada: Lat {round(lat, 4)}, Lon {round(lon, 4)}"
