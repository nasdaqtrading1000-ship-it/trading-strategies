"""Consume recortes de Acumulacion, con reconciliacion por fill y deduplicacion.

El host proporciona sus funciones de broker; importar este modulo no opera.
"""
import json
import math
import urllib.parse
from datetime import datetime, timezone, timedelta

TXT = 'Acumulacion.txt'
TERMINAL = {'filled', 'canceled', 'expired', 'rejected', 'done_for_day'}


def event_valid(event, now):
    if event.get('txt_name') != TXT or event.get('schema_version') != 1:
        return False
    if not str(event.get('event_id', '')).startswith('ACU-RISK-'):
        return False
    fraction = float(event.get('fraction', 0))
    if not math.isfinite(fraction) or not any(abs(fraction-x)<1e-9 for x in (.25, 1/3, .5)):
        return False
    at = datetime.fromisoformat(event['occurred_at'].replace('Z', '+00:00'))
    if at.tzinfo is None or not now-timedelta(days=7) <= at <= now+timedelta(minutes=1):
        return False
    return isinstance(event.get('targets'), list) and len(event['targets']) <= 20000


def process_reductions(host, conn, config, broker, events, mode):
    """Devuelve claves bloqueadas por ordenes pendientes; nunca marca fill al enviar."""
    conn.execute('CREATE TABLE IF NOT EXISTS replicated_reductions (event_id TEXT, symbol TEXT, status TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(event_id,symbol))')
    conn.commit()
    if TXT not in config.get('selected_txt_names', []):
        return set(), []
    now = datetime.now(timezone.utc)
    errors = []
    blocked = set()
    for event in events:
        if not event_valid(event, now):
            continue
        keys = {t['operation_key'] for t in event['targets'] if t.get('operation_key')}
        if not keys:
            continue
        # Leer todas las filas del evento, incluidas las pendientes del ciclo anterior.
        existing = {r['symbol']:r for r in conn.execute('SELECT * FROM replicated_reductions WHERE event_id=?', (event['event_id'],)).fetchall()}
        origins = [dict(r) for r in conn.execute('''SELECT p.* FROM replicated_positions p
                     JOIN replicated_operations o ON o.replication_id=p.open_replication_id
                     WHERE p.status='OPEN' AND p.txt_name=? AND o.broker_mode=?''', (TXT, mode)).fetchall()
                   if r['operation_key'] in keys]
        groups = {}
        for row in origins:
            groups.setdefault(row['symbol'], []).append(row)
        for symbol in sorted(set(groups) | set(existing)):
            saved = existing.get(symbol)
            data = json.loads(saved['payload']) if saved else None
            if saved and saved['status'] in ('COMPLETE', 'NO_HOLDINGS'):
                continue
            rows = data['rows'] if data else groups.get(symbol, [])
            rowkeys = {r['operation_key'] for r in rows}
            if blocked & rowkeys:
                continue
            try:
                if not data:
                    if not rows:
                        continue
                    if any(str(r['direction']).upper() not in ('LONG','BUY','COMPRA') for r in rows):
                        raise RuntimeError('Recorte solo LONG: '+symbol)
                    quantity = sum(float(r['shares']) for r in rows)
                    if mode != 'paper':
                        # Excluir las posiciones paper heredadas al contrastar LIVE.
                        net = conn.execute('''SELECT SUM(CASE WHEN p.direction='SHORT' THEN -p.shares ELSE p.shares END)
                          FROM replicated_positions p JOIN replicated_operations o ON o.replication_id=p.open_replication_id
                          WHERE p.status='OPEN' AND p.symbol=? AND o.broker_mode=?''', (symbol,mode)).fetchone()[0] or 0
                        actual = broker.position(symbol)
                        available = float((actual or {}).get('qty') or 0)
                        if not actual or actual.get('side') != 'long' or abs(available-net)>max(2e-6,abs(available)*1e-6):
                            raise RuntimeError('Recorte pendiente de reconciliar cantidades: '+symbol)
                        # No cerrar una apertura que aun no esta ejecutada.
                        for row in rows:
                            opening = conn.execute('SELECT broker_order_id FROM replicated_operations WHERE replication_id=?', (row['open_replication_id'],)).fetchone()
                            order = broker._request('GET', '/v2/orders/'+opening[0])
                            if order.get('status') != 'filled':
                                raise RuntimeError('Apertura pendiente de fill: '+symbol)
                    target = float(host.format_qty(quantity * float(event['fraction'])))
                    if target <= 0:
                        raise RuntimeError('Recorte inferior a precision del broker')
                    synthetic = dict(rows[0], operation_key=event['event_id']+'|'+symbol,
                                     current_price=float(rows[0]['open_price']), entry_price=float(rows[0]['open_price']))
                    data = dict(rows=rows, target_qty=target, applied_qty=0., order_id=None,
                                attempted=False, synthetic=synthetic)
                    conn.execute('INSERT INTO replicated_reductions VALUES (?,?,?,?)',
                                 (event['event_id'],symbol,'PREPARED',json.dumps(data)))
                    conn.commit()
                synthetic = data['synthetic']
                client_id = host.alpaca_client_order_id(host.replication_id(synthetic, 'CLOSE'), config.get('order_generation',''))
                order = None
                if mode != 'paper':
                    try:
                        order = broker._request('GET','/v2/orders:by_client_order_id?'+urllib.parse.urlencode({'client_order_id':client_id}))
                    except RuntimeError as e:
                        if 'HTTP 404' not in str(e):
                            raise
                        if data['attempted']:
                            raise RuntimeError('Envio anterior incierto: revisar '+symbol)
                if order is None:
                    data['attempted'] = True
                    conn.execute('UPDATE replicated_reductions SET status=?,payload=? WHERE event_id=? AND symbol=?',
                                 ('SUBMITTING',json.dumps(data),event['event_id'],symbol));conn.commit()
                    result = broker.place_order(synthetic,'CLOSE',0,data['target_qty'])
                    data['order_id'] = result.broker_order_id
                    if mode == 'paper':
                        order = dict(id=result.broker_order_id,status='filled',filled_qty=str(data['target_qty']))
                    else:
                        order = broker._request('GET','/v2/orders/'+result.broker_order_id)
                data['order_id'] = order['id']
                filled = float(order.get('filled_qty') or 0)
                if not math.isfinite(filled) or filled < data['applied_qty'] or filled > data['target_qty']+1e-8:
                    raise RuntimeError('Cantidad ejecutada invalida en '+symbol)
                delta = filled-data['applied_qty']
                if delta > 0:
                    original = sum(float(r['shares']) for r in data['rows'])
                    conn.execute('BEGIN IMMEDIATE')
                    for row in data['rows']:
                        current = conn.execute("SELECT shares,status FROM replicated_positions WHERE operation_key=?",(row['operation_key'],)).fetchone()
                        expected = float(row['shares'])*(1-data['applied_qty']/original)
                        if not current or current['status']!='OPEN' or abs(float(current['shares'])-expected)>1e-7:
                            raise RuntimeError('Entrada cambio durante cierre parcial: '+symbol)
                        conn.execute('UPDATE replicated_positions SET shares=? WHERE operation_key=?',
                                     (float(row['shares'])*(1-filled/original),row['operation_key']))
                    data['applied_qty'] = filled
                complete = order['status']=='filled' and abs(filled-data['target_qty'])<1e-7
                status = 'COMPLETE' if complete else ('NEEDS_REVIEW' if order['status'] in TERMINAL else 'PENDING')
                conn.execute('UPDATE replicated_reductions SET status=?,payload=? WHERE event_id=? AND symbol=?',
                             (status,json.dumps(data),event['event_id'],symbol));conn.commit()
                if not complete:
                    blocked.update(rowkeys)
                    if status=='NEEDS_REVIEW':
                        errors.append(dict(symbol=symbol,event_id=event['event_id'],error='Cierre no completado: '+order['status']))
            except Exception as e:
                conn.rollback()
                blocked.update(rowkeys)
                errors.append(dict(symbol=symbol,event_id=event['event_id'],error=str(e)))
    return blocked, errors
