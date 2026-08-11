"""Script para probar la función de polling una vez y ver resultados.
Ejecutar: python run_poll.py

Nota: descarga y parsea los PDFs completos del Boletín Nacional (Primera
Sección) y de la Provincia (sección OFICIAL), así que puede tardar unos
segundos.
"""
from app import poll_once, data_store
import json
import os

if __name__ == '__main__':
    print('Ejecutando poll_once()...')
    poll_once()
    out = {
        'last_checked': data_store.get('last_checked'),
        'provincial_edition': data_store.get('provincial', {}).get('edition'),
        'provincial_matches': data_store.get('provincial', {}).get('matches', []),
        'national_edition': data_store.get('national', {}).get('edition'),
        'national_matches': data_store.get('national', {}).get('matches', []),
        'summary': data_store.get('national', {}).get('summary', ''),
        'last_new_items': data_store.get('last_new_items', [])
    }
    print('Resultados:')
    print(json.dumps(out, ensure_ascii=False, indent=2))
    try:
        with open('last_run.json', 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            print('Guardado en last_run.json')
    except Exception as e:
        print('No se pudo guardar last_run.json:', e)
