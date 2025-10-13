import eventlet
eventlet.monkey_patch()
from flask import Flask, stream_with_context, request, jsonify, render_template, redirect, url_for, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta
from flask_migrate import Migrate
from flask_wtf.csrf import generate_csrf
from flask_socketio import SocketIO, emit, join_room
from urllib.parse import urlparse, urlunparse
from flask_cors import CORS
import threading
import time
import json
import os
from collections import defaultdict, deque
from sqlalchemy import text  
import logging, re
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
CORS(app)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///healthcare.db').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')
app.config['CAMERA_STREAM_URL'] = 'http://192.168.0.102:81/stream'
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_recycle': 300,
    'pool_pre_ping': True,
    'pool_size': 10,
    'max_overflow': 20,
}
message_queues = defaultdict(lambda: deque(maxlen=200))
connected_clients = defaultdict(list)
sse_clients = []
db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

pending_updates = {}

# Database Models
class Nurse(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cpr = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(100))
    phone_number = db.Column(db.String(20))
    municipality = db.Column(db.String(100))
    password = db.Column(db.String(255))
    is_admin = db.Column(db.Boolean, default=False)

class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cpr = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(100))
    address = db.Column(db.String(255))
    doctor_name = db.Column(db.String(100))
    clinic_id = db.Column(db.String(50))
    municipality = db.Column(db.String(100))
    emergency_info = db.Column(db.Text)
    psychological_info = db.Column(db.Text)
    last_updated = db.Column(db.DateTime)
    ip_address = db.Column(db.String(20))
    camera_url = db.Column(db.String(255))
    # NEW: Device name to reach the ESP32 lock via SSE (/stream?client_id=<device_name>)
    device_name = db.Column(db.String(64))  # e.g., "door01"

class Medicine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    name = db.Column(db.String(100))
    side_effect = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    # Use simple string fields instead of complex types
    schedule_type = db.Column(db.String(20), default='daily')
    days = db.Column(db.String(200), default='')  # Store as comma-separated string
    times = db.Column(db.String(200), default='')  # Store as comma-separated string
    
    def get_days_list(self):
        """Get days as Python list"""
        if self.days:
            return [d.strip() for d in self.days.split(',')]
        return []
    
    def get_times_list(self):
        """Get times as Python list"""
        if self.times:
            return [t.strip() for t in self.times.split(',')]
        return []
    
    def set_days_list(self, days):
        """Set days from Python list - store as comma-separated string"""
        if days and isinstance(days, list):
            self.days = ', '.join([str(day) for day in days])
        else:
            self.days = ''
    
    def set_times_list(self, times):
        """Set times from Python list - store as comma-separated string"""
        if times and isinstance(times, list):
            self.times = ', '.join([str(time) for time in times])
        else:
            self.times = ''
            
            
class MedTime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    med_id = db.Column(db.String(50))
    time = db.Column(db.String(20))
    date = db.Column(db.String(20))
    active = db.Column(db.Boolean, default=True)
    taken = db.Column(db.Boolean, default=False)

class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    rem_id = db.Column(db.String(50))
    date = db.Column(db.String(20))
    note = db.Column(db.Text)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    user_name = db.Column(db.String(100))
    date_time = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.Text)
    urgency = db.Column(db.String(20))
    hidden = db.Column(db.Boolean, default=False)
    client = db.relationship('Client', backref=db.backref('notifications', lazy=True))

class Visit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'))
    user_name = db.Column(db.String(100))
    nurse_id = db.Column(db.Integer, db.ForeignKey('nurse.id'))
    nurse_name = db.Column(db.String(100))
    date = db.Column(db.String(20))
    time = db.Column(db.String(20))
    next_visit_date = db.Column(db.String(20))
    notes = db.Column(db.Text)
    
def normalize_room(s: str) -> str:
    if not s:
        return ""
    return re.sub(r'[^A-Za-z0-9]+', '', s).lower()

def camera_stream_url(raw: str | None) -> str | None:
    if not raw:
        return None
    u = raw.strip()
    # If they already stored a full stream URL, keep it.
    if u.endswith('/stream') or u.endswith(':81/stream'):
        return u
    # If they stored a plain host (with or without http), build :81/stream
    if not u.startswith('http://') and not u.startswith('https://'):
        u = 'http://' + u
    p = urlparse(u)
    # Default to port 81 and path /stream if none supplied
    netloc = p.hostname
    if p.port:
        netloc = f"{p.hostname}:{p.port}"
    else:
        netloc = f"{p.hostname}:81"
    return urlunparse((p.scheme, netloc, '/stream', '', '', ''))
    #return raw.strip()
# Online clients tracking
online_clients = {}  # {client_cpr: last_heartbeat_timestamp}

# Add near the top with other socketio events
@socketio.on('client_online')
def handle_client_online(data):
    client_cpr = data.get('client_id')
    if client_cpr:
        online_clients[client_cpr] = time.time()
        print(f"💚 WebSocket: Client {client_cpr} is online")
        # Broadcast to all dashboard users
        socketio.emit('client_status_update', {
            'client_cpr': client_cpr,
            'status': 'online'
        })

@socketio.on('client_offline')
def handle_client_offline(data):
    client_cpr = data.get('client_id')
    if client_cpr and client_cpr in online_clients:
        del online_clients[client_cpr]
        print(f"🔴 WebSocket: Client {client_cpr} is offline")
        # Broadcast to all dashboard users
        socketio.emit('client_status_update', {
            'client_cpr': client_cpr,
            'status': 'offline'
        })

@app.route('/api/client_heartbeat', methods=['POST'])
def client_heartbeat():
    """Client sends heartbeat to indicate they're online; safely upsert client."""
    data = request.json or {}
    client_cpr = (data.get('client_id') or '').strip()
    if not client_cpr:
        return jsonify({'status': 'error', 'message': 'missing client_id'}), 400

    name         = (data.get('name') or '').strip()
    municipality = (data.get('municipality') or '').strip()  # may be ''
    if municipality.lower() == 'all':
        # "all" is reserved for Nurses/admin filters; don't persist it on Clients
        municipality = ''

    try:
        client = Client.query.filter_by(cpr=client_cpr).first()
        if not client:
            client = Client(
                cpr=client_cpr,
                name=name or client_cpr,
                municipality=municipality,   # '' okay; can be filled later
                last_updated=datetime.now(),
                ip_address=request.remote_addr
            )
            db.session.add(client)
            db.session.commit()
        else:
            changed = False
            # Only update name if provided and different
            if name and name != client.name:
                client.name = name
                changed = True
            # Only update municipality if provided AND not "all"
            if municipality and municipality != client.municipality:
                client.municipality = municipality
                changed = True
            if changed:
                client.last_updated = datetime.now()
                db.session.commit()
    except Exception as e:
        app.logger.error(f"Heartbeat upsert error for {client_cpr}: {e}")

    # Maintain online status + notify dashboards on first seen
    was_online = client_cpr in online_clients
    online_clients[client_cpr] = time.time()
    if not was_online:
        socketio.emit('client_status_update', {
            'client_cpr': client_cpr,
            'status': 'online'
        })

    return jsonify({'status': 'success'})


# Update the cleanup function to emit offline events
def cleanup_old_heartbeats():
    """Remove clients that haven't sent heartbeat in the last 60 seconds"""
    current_time = time.time()
    offline_clients = []
    for client_cpr, last_heartbeat in online_clients.items():
        if current_time - last_heartbeat > 60:  # 1 minute timeout
            offline_clients.append(client_cpr)
    
    for client_cpr in offline_clients:
        del online_clients[client_cpr]
        print(f"🔴 Cleanup: Client {client_cpr} marked as offline")
        # Emit offline status
        socketio.emit('client_status_update', {
            'client_cpr': client_cpr,
            'status': 'offline'
        })

@app.route('/api/client/<string:client_cpr>/medicines')
def get_client_medicines_public(client_cpr):
    """Public endpoint for clients to get their own medicines"""
    try:
        app.logger.info(f"📋 Medicine request for CPR: {client_cpr}")
        
        # Add timeout protection
        start_time = time.time()
        
        # Find client by CPR with timeout protection
        client = Client.query.filter_by(cpr=client_cpr).first()
        if not client:
            app.logger.warning(f"❌ Client not found: {client_cpr}")
            return jsonify({'status': 'error', 'message': 'Client not found'}), 404
        
        # Get medicines with optimized query
        medicines = Medicine.query.filter_by(client_id=client.id).all()
        
        processing_time = time.time() - start_time
        app.logger.info(f"✅ Serving {len(medicines)} medicines for {client_cpr} in {processing_time:.2f}s")
        
        # If query takes too long, log warning
        if processing_time > 2.0:
            app.logger.warning(f"⚠️ Slow medicine query for {client_cpr}: {processing_time:.2f}s")
        
        medicine_list = []
        for med in medicines:
            medicine_data = {
                'id': med.id,
                'name': med.name,
                'side_effect': med.side_effect or '',
                'active': med.active,
                'schedule_type': med.schedule_type,
                'days': med.get_days_list(),
                'times': med.get_times_list()
            }
            medicine_list.append(medicine_data)
        
        return jsonify({
            'status': 'success',
            'client_id': client.id,
            'client_name': client.name,
            'medicines': medicine_list,
            'processing_time': f"{processing_time:.2f}s"
        })
        
    except Exception as e:
        app.logger.error(f"🔴 Error in medicine endpoint: {str(e)}")
        return jsonify({'status': 'error', 'message': 'Internal server error'}), 500


@app.route('/health')
def health_check():
    """Simple health check endpoint"""
    try:
        # Test database connection with explicit text()
        db.session.execute(text('SELECT 1'))
        return jsonify({'status': 'healthy', 'database': 'connected'})
    except Exception as e:
        app.logger.error(f"Health check failed: {e}")
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500

# Login Manager
@login_manager.user_loader
def load_user(user_id):
    return Nurse.query.get(int(user_id))

# WebSocket (browser) — unchanged
@socketio.on('connect')
def handle_connect():
    app.logger.info(f"[WS] connect sid={request.sid}")
    emit('connected', {'status': 'ok'})

@socketio.on('client_hello')
def handle_client_hello(data):
    client_id_raw = (data or {}).get('client_id', '')
    room_name = normalize_room(client_id_raw)
    app.logger.info(f"[WS] client_hello raw='{client_id_raw}' room='{room_name}' sid={request.sid}")
    if room_name:
        join_room(room_name)
        app.logger.info(f"[WS] joined room='{room_name}' sid={request.sid}")
        emit('hello_ack', {'ok': True, 'room': room_name})

#connection tracking
client_connections = {}

@app.route('/stream')
def stream():
    client_id = (request.args.get('client_id') or '').strip()
    
    if not client_id:
        app.logger.warning(f"[SSE] missing client_id from {request.remote_addr} UA={request.user_agent}")
        return "missing client_id", 400

    # Track connection
    connection_id = f"{client_id}_{int(time.time())}"
    client_connections[connection_id] = {
        'client_id': client_id,
        'start_time': time.time(),
        'remote_addr': request.remote_addr
    }
    
    @stream_with_context
    def event_stream():
        try:
            yield f"data: {json.dumps({'type': 'connected', 'client_id': client_id})}\n\n"
            
            last_keep = time.time()
            while True:
                try:
                    q = message_queues[client_id]
                    if q:
                        msg = q.popleft()
                        yield f"data: {json.dumps(msg)}\n\n"
                        last_keep = time.time()
                    else:
                        if time.time() - last_keep > 30:
                            yield ": keepalive\n\n"
                            last_keep = time.time()
                        time.sleep(1)
                except Exception as e:
                    app.logger.error(f"[SSE] Error for {client_id}: {e}")
                    break
        finally:
            # Clean up on disconnect
            if connection_id in client_connections:
                del client_connections[connection_id]
            app.logger.info(f"[SSE] Connection closed for {client_id}")

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
    }
    return Response(event_stream(), headers=headers)

# endpoint to check connection status
@app.route('/api/connection_status')
def connection_status():
    return jsonify({
        'active_connections': len(client_connections),
        'connections': client_connections,
        'message_queues': {k: len(v) for k, v in message_queues.items()},
        'online_clients': online_clients
    }) 
    
# Camera test endpoints (kept)
@app.post('/client/<int:client_id>/camera/open')
@login_required
def camera_open(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status':'error','message':'Access denied'}), 403

    stream_url = camera_stream_url(client.camera_url)
    if not stream_url:
        return jsonify({'status':'error','message':'Camera URL not set for this client'}), 400

    message_queues[client.cpr].append({
        'type': 'servo_set',
        'target': client.cpr,
        'angle': 180,
        'ts': datetime.utcnow().isoformat()
    })
    app.logger.info(f"[SSE] queued servo_set(180) for '{client.cpr}'  qlen={len(message_queues[client.cpr])}")
    return jsonify({'ok': True, 'stream_url': stream_url})

@app.post('/client/<int:client_id>/camera/close')
@login_required
def camera_close(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status':'error','message':'Access denied'}), 403
    message_queues[client.cpr].append({
        'type': 'servo_set',
        'target': client.cpr,
        'angle': 0,
        'ts': datetime.utcnow().isoformat()
    })
    app.logger.info(f"[SSE] queued servo_set(0) for '{client.cpr}'  qlen={len(message_queues[client.cpr])}")
    return jsonify({'ok': True})

# ✅ NEW: Lock control endpoints (no timer) — targets client.device_name
@app.post('/client/<int:client_id>/lock/open')
@login_required
def lock_open(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status':'error','message':'Access denied'}), 403
    device = (client.device_name or '').strip() or (client.cpr or '').strip()
    if not device:
        return jsonify({'status':'error','message':'No device_name (or CPR) configured for this client'}), 400
    message_queues[device].append({
        'type': 'lock',
        'action': 'open',
        'target': device,
        'ts': datetime.utcnow().isoformat()
    })
    app.logger.info(f"[SSE] queued lock OPEN for '{device}' qlen={len(message_queues[device])}")
    return jsonify({'ok': True})

@app.post('/client/<int:client_id>/lock/close')
@login_required
def lock_close(client_id):
    client = Client.query.get_or_404(client_id)
    # typo fixed: municipality
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status':'error','message':'Access denied'}), 403
    device = (client.device_name or '').strip() or (client.cpr or '').strip()
    if not device:
        return jsonify({'status':'error','message':'No device_name (or CPR) configured for this client'}), 400
    message_queues[device].append({
        'type': 'lock',
        'action': 'close',
        'target': device,
        'ts': datetime.utcnow().isoformat()
    })
    app.logger.info(f"[SSE] queued lock CLOSE for '{device}' qlen={len(message_queues[device])}")
    return jsonify({'ok': True})


@app.after_request
def set_csrf_cookie(resp):
    resp.set_cookie('csrf_token', generate_csrf(), samesite='Lax')
    return resp

# Notifications (unchanged)
@app.route('/api/notification', methods=['POST'])
def receive_notification():
    data = request.json
    if not data or 'client_id' not in data:
        return jsonify({'status': 'error', 'message': 'Invalid data'}), 400

    client = Client.query.filter_by(cpr=data['client_id']).first()
    if not client:
        return jsonify({'status': 'error', 'message': 'Client not found'}), 404

    notification = Notification(
        client_id=client.id,
        user_name=data.get('user_name', ''),
        note=data.get('note', ''),
        urgency=data.get('urgency', 'normal'),
        date_time=datetime.utcnow()
    )
    db.session.add(notification)
    db.session.commit()

    socketio.emit('new_notification', {
        'client_id': client.id,
        'notification': {
            'id': notification.id,
            'urgency': notification.urgency,
            'note': notification.note,
            'date_time': notification.date_time.isoformat(),
            'user_name': notification.user_name
        }
    }, namespace='/')

    return jsonify({'status': 'success'})

@app.route('/notification/<int:notification_id>')
@login_required
def get_notification(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    return jsonify({
        'id': notification.id,
        'user_name': notification.user_name,
        'date_time': notification.date_time.isoformat(),
        'urgency': notification.urgency,
        'note': notification.note,
        'client_id': notification.client_id
    })

@app.route('/notification/<int:notification_id>/hide', methods=['POST'])
@login_required
def hide_notification(notification_id):
    notification = Notification.query.get_or_404(notification_id)
    notification.hidden = True
    db.session.commit()
    return jsonify({'status': 'success'})

@app.post('/client/<int:client_id>/servo/toggle')
@login_required
def toggle_client_servo(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    if not client.cpr:
        return jsonify({'status': 'error', 'message': 'Client has no CPR/room id configured'}), 400

    message_queues[client.cpr].append({
        "type": "servo_toggle",
        "target": client.cpr,
        "ts": datetime.utcnow().isoformat()
    })
    app.logger.info(f"[SSE] queued servo_toggle for target='{client.cpr}' qlen={len(message_queues[client.cpr])}")
    return jsonify({'status': 'ok'})

# Sync Endpoints
@app.route('/api/full_sync', methods=['POST'])
def full_sync():
    try:
        data = request.json
        client_cpr = data['user']['cpr']
        _muni = (data['user'].get('municipality', '') or '').strip()
        if _muni.lower() == 'all':
            _muni = ''  # "all" is reserved for nurse filter, never store on Client

        client = Client.query.filter_by(cpr=client_cpr).first()
        if not client:
            client = Client(
                cpr=client_cpr,
                name=data['user'].get('name', ''),
                address=data['user'].get('address', ''),
                doctor_name=data['user'].get('doctor_name', ''),
                clinic_id=data['user'].get('clinic_id', ''),
                municipality=_muni,
                emergency_info=data['user'].get('emergency_info', ''),
                psychological_info=data['user'].get('psychological_info', ''),
                last_updated=datetime.now()
            )
            db.session.add(client)
            db.session.commit()

        # Clear existing data
        Medicine.query.filter_by(client_id=client.id).delete()
        
        # Sync medicines with schedule fields
        for med_data in data['full_data'].get('medicines', []):
            medicine = Medicine(
                client_id=client.id,
                name=med_data['name'],
                side_effect=med_data.get('side_effect', ''),
                active=med_data.get('active', True),
                schedule_type=med_data.get('schedule_type', 'daily')
            )
            
            # Set schedule fields
            medicine.set_days_list(med_data.get('days', []))
            medicine.set_times_list(med_data.get('times', []))
            
            db.session.add(medicine)
        
        # Rest of your sync code...
        MedTime.query.filter_by(client_id=client.id).delete()
        Reminder.query.filter_by(client_id=client.id).delete()
        
        for mt_data in data['full_data'].get('med_times', []):
            db.session.add(MedTime(
                client_id=client.id,
                med_id=mt_data.get('id', ''),
                time=mt_data.get('time', ''),
                date=mt_data.get('date', ''),
                active=mt_data.get('active', True),
                taken=mt_data.get('taken', False)
            ))
        
        db.session.commit()
        return jsonify({'status': 'success'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
        
@app.route('/medicine/<int:medicine_id>', methods=['PUT', 'DELETE'])
@app.route('/client/<int:client_id>/medicines', methods=['POST'])
@login_required
def medicine_updates(client_id=None, medicine_id=None):
    """Handle medicine updates and notify client"""
    # ... existing medicine update logic ...
    
    # After successful update, notify the client
    client = Client.query.get(client_id) if client_id else Client.query.get(medicine.client_id)
    if client and client.cpr:
        message_queues[client.cpr].append({
            'type': 'sync',
            'message': 'Medicine data updated',
            'timestamp': datetime.utcnow().isoformat()
        })
        app.logger.info(f"📢 Notified client {client.cpr} about medicine update")
    
    return jsonify({'status': 'success'})
        

@app.route('/api/sync_changes', methods=['POST'])
def sync_changes():
    try:
        data = request.json
        if not data or 'user' not in data or 'changes' not in data:
            return jsonify({'status': 'error', 'message': 'Invalid data'}), 400
        
        _muni = (data['user'].get('municipality', '') or '').strip()
        if _muni.lower() == 'all':
            _muni = ''  # "all" is reserved for nurse filter, never store on Client

        
        client_cpr = data['user']['cpr']
        client = Client.query.filter_by(cpr=client_cpr).first()
        
        if not client:
            client = Client(
                cpr=client_cpr,
                name=data['user'].get('name', ''),
                address=data['user'].get('address', ''),
                doctor_name=data['user'].get('doctor_name', ''),
                clinic_id=data['user'].get('clinic_id', ''),
                municipality=_muni,
                emergency_info=data['user'].get('emergency_info', ''),
                psychological_info=data['user'].get('psychological_info', ''),
                last_updated=datetime.now(),
                ip_address=request.remote_addr
            )
            db.session.add(client)
            db.session.commit()

        changes = data['changes']
        
        # Handle medicines sync - only sync from client to server
        for med_data in changes.get('medicines', {}).get('new', []):
            med = Medicine(
                client_id=client.id,
                name=med_data['name'],
                side_effect=med_data.get('side_effect', ''),
                active=med_data.get('active', True)
            )
            # Add schedule fields if present
            if 'schedule_type' in med_data:
                med.schedule_type = med_data['schedule_type']
            if 'days' in med_data:
                med.days = med_data['days']
            if 'times' in med_data:
                med.times = med_data['times']
            db.session.add(med)
        
        for med_data in changes.get('medicines', {}).get('modified', []):
            med = Medicine.query.filter_by(
                client_id=client.id,
                name=med_data['name']
            ).first()
            if med:
                med.side_effect = med_data.get('side_effect', med.side_effect)
                med.active = med_data.get('active', med.active)
                # Update schedule fields if present
                if 'schedule_type' in med_data:
                    med.schedule_type = med_data['schedule_type']
                if 'days' in med_data:
                    med.days = med_data['days']
                if 'times' in med_data:
                    med.times = med_data['times']
        
        for med_data in changes.get('medicines', {}).get('deleted', []):
            Medicine.query.filter_by(
                client_id=client.id,
                name=med_data['name']
            ).delete()
        
        db.session.commit()
        
        socketio.emit('data_updated', {
            'client_id': client.id,
            'update_type': 'changes_sync',
            'timestamp': datetime.utcnow().isoformat()
        }, namespace='/')
        
        return jsonify({'status': 'success'})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Server-side medicine management page
@app.route('/client/<int:client_id>/manage_medicines')
@login_required
def manage_client_medicines(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("You don't have access to this client", "error")
        return redirect(url_for('dashboard'))
    
    medicines = Medicine.query.filter_by(client_id=client.id).all()
    return render_template('manage_medicines.html', 
                         client=client, 
                         medicines=medicines)

# --- DELETE CLIENT (with cascade-like cleanup) ---
@app.post('/client/<int:client_id>/delete')
@login_required
def delete_client(client_id):
    client = Client.query.get_or_404(client_id)

    # Access control: same municipality or admin
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for('dashboard'))

    try:
        # Remove related data first (FKs aren't declared with cascade here)
        Medicine.query.filter_by(client_id=client.id).delete()
        MedTime.query.filter_by(client_id=client.id).delete()
        Reminder.query.filter_by(client_id=client.id).delete()
        Notification.query.filter_by(client_id=client.id).delete()
        Visit.query.filter_by(client_id=client.id).delete()

        # Finally remove the client
        db.session.delete(client)
        db.session.commit()
        flash("Client deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting client: {e}", "error")

    return redirect(url_for('dashboard'))



# Add medicine from server UI
@app.route('/client/<int:client_id>/medicines/add', methods=['GET', 'POST'])
@login_required
def add_medicine_server(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        name = request.form.get('name')
        side_effect = request.form.get('side_effect', '')
        active = 'active' in request.form
        schedule_type = request.form.get('schedule_type', 'daily')
        
        # Parse days for weekly schedule
        days = []
        if schedule_type == 'weekly':
            days = request.form.getlist('days')
        
        # Parse times
        times_input = request.form.get('times', '')
        times = [t.strip() for t in times_input.split(',') if t.strip()]
        
        # Validate required fields
        if not name:
            flash("Medicine name is required", "error")
            return render_template('add_medicine.html', client=client)
        
        # Create medicine
        medicine = Medicine(
            client_id=client.id,
            name=name,
            side_effect=side_effect,
            active=active,
            schedule_type=schedule_type
        )
        
        # Set schedule fields using the new methods
        medicine.set_days_list(days)
        medicine.set_times_list(times)
        
        try:
            db.session.add(medicine)
            db.session.commit()
            flash("Medicine added successfully", "success")
            return redirect(url_for('manage_client_medicines', client_id=client.id))
        except Exception as e:
            db.session.rollback()
            flash(f"Error adding medicine: {str(e)}", "error")
            return render_template('add_medicine.html', client=client)
    
    return render_template('add_medicine.html', client=client)
    

# Edit medicine from server UI
@app.route('/medicine/<int:medicine_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_medicine_server(medicine_id):
    medicine = Medicine.query.get_or_404(medicine_id)
    client = Client.query.get(medicine.client_id)
    
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        medicine.name = request.form.get('name', medicine.name)
        medicine.side_effect = request.form.get('side_effect', medicine.side_effect)
        medicine.active = 'active' in request.form
        medicine.schedule_type = request.form.get('schedule_type', 'daily')
        
        # Parse days for weekly schedule - store as comma-separated string
        if medicine.schedule_type == 'weekly':
            days = request.form.getlist('days')
            medicine.set_days_list(days)
        else:
            medicine.days = ''  # Clear days for daily schedule
        
        # Parse times - store as comma-separated string
        times_input = request.form.get('times', '')
        times = [t.strip() for t in times_input.split(',') if t.strip()]
        medicine.set_times_list(times)
        
        try:
            db.session.commit()
            flash("Medicine updated successfully", "success")
            return redirect(url_for('manage_client_medicines', client_id=client.id))
        except Exception as e:
            db.session.rollback()
            flash(f"Error updating medicine: {str(e)}", "error")
            return render_template('edit_medicine.html', 
                                 medicine=medicine, 
                                 client=client)
    
    return render_template('edit_medicine.html', 
                         medicine=medicine, 
                         client=client)

# Delete medicine from server UI
@app.route('/medicine/<int:medicine_id>/delete', methods=['POST'])
@login_required
def delete_medicine_server(medicine_id):
    medicine = Medicine.query.get_or_404(medicine_id)
    client = Client.query.get(medicine.client_id)
    
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for('dashboard'))
    
    db.session.delete(medicine)
    db.session.commit()
    flash("Medicine deleted successfully", "success")
    return redirect(url_for('manage_client_medicines', client_id=client.id))


@app.route('/api/find_client')
def find_client():
    cpr = request.args.get('cpr')
    if not cpr:
        return jsonify({'status': 'error', 'message': 'CPR required'}), 400
    
    client = Client.query.filter_by(cpr=cpr).first()
    if not client:
        return jsonify({'status': 'error', 'message': 'Client not found'}), 404
    
    return jsonify({
        'status': 'success',
        'id': client.id,
        'name': client.name,
        'cpr': client.cpr
    })

@app.route('/client/<int:client_id>/visits', methods=['GET', 'POST'])
@login_required
def handle_client_visits(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    if request.method == 'GET':
        # Get visits
        visits = Visit.query.filter_by(client_id=client.id).order_by(Visit.date.desc()).all()
        return jsonify({
            'status': 'success',
            'visits': [{
                'id': visit.id,
                'nurse_name': visit.nurse_name,
                'date': visit.date,
                'time': visit.time,
                'next_visit_date': visit.next_visit_date,
                'notes': visit.notes
            } for visit in visits]
        })
    
    elif request.method == 'POST':
        # Add new visit
        data = request.json
        if not data:
            return jsonify({'status': 'error', 'message': 'No data provided'}), 400
        
        # Validate required fields
        if not data.get('date') or not data.get('time'):
            return jsonify({'status': 'error', 'message': 'Date and time are required'}), 400
        
        visit = Visit(
            client_id=client.id,
            user_name=client.name,
            nurse_id=current_user.id,
            nurse_name=current_user.name,
            date=data['date'],
            time=data['time'],
            next_visit_date=data.get('next_visit_date'),
            notes=data.get('notes', '')
        )
        
        db.session.add(visit)
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': 'Visit added successfully',
            'visit': {
                'id': visit.id,
                'nurse_name': visit.nurse_name,
                'date': visit.date,
                'time': visit.time,
                'next_visit_date': visit.next_visit_date,
                'notes': visit.notes
            }
        })

# Auth / Admin / Client routes (unchanged except edit_client adds device_name)
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        cpr = request.form.get('cpr')
        password = request.form.get('password')
        nurse = Nurse.query.filter_by(cpr=cpr).first()
        
        if nurse and check_password_hash(nurse.password, password):
            login_user(nurse)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))
        flash('Invalid CPR or password')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/admin/create_user', methods=['GET', 'POST'])
@login_required
def create_user():
    if not current_user.is_admin:
        flash('Admin access required')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        cpr = request.form.get('cpr')
        name = request.form.get('name')
        phone = request.form.get('phone')
        municipality = request.form.get('municipality')
        password = generate_password_hash(request.form.get('password'))
        is_admin = 'is_admin' in request.form
        
        if Nurse.query.filter_by(cpr=cpr).first():
            flash('Nurse with this CPR already exists')
        else:
            nurse = Nurse(
                cpr=cpr, name=name, phone_number=phone,
                municipality=municipality, password=password,
                is_admin=is_admin
            )
            db.session.add(nurse)
            db.session.commit()
            flash('Nurse account created successfully')
            return redirect(url_for('manage_users'))
    
    return render_template('create_user.html')

@app.route('/admin/manage_users')
@login_required
def manage_users():
    if not current_user.is_admin:
        flash('Admin access required')
        return redirect(url_for('dashboard'))
    
    nurses = Nurse.query.all()
    return render_template('manage_users.html', nurses=nurses)

@app.route('/admin/edit_user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required', 'error')
        return redirect(url_for('dashboard'))
    
    nurse = Nurse.query.get_or_404(user_id)
    
    if request.method == 'POST':
        nurse.cpr = request.form.get('cpr', nurse.cpr)
        nurse.name = request.form.get('name', nurse.name)
        nurse.phone_number = request.form.get('phone', nurse.phone_number)
        nurse.municipality = request.form.get('municipality', nurse.municipality)
        nurse.is_admin = 'is_admin' in request.form
        
        if request.form.get('password'):
            nurse.password = generate_password_hash(request.form.get('password'))
        
        db.session.commit()
        flash('User updated successfully', 'success')
        return redirect(url_for('manage_users'))
    
    return render_template('edit_user.html', nurse=nurse)

@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if not current_user.is_admin:
        flash('Admin access required', 'error')
        return redirect(url_for('manage_users'))
    
    if current_user.id == user_id:
        flash('You cannot delete your own account', 'error')
        return redirect(url_for('manage_users'))
    
    nurse = Nurse.query.get_or_404(user_id)
    db.session.delete(nurse)
    db.session.commit()
    flash('User deleted successfully', 'success')
    return redirect(url_for('manage_users'))

@app.route('/client/<int:client_id>/notifications')
@login_required
def notification_history(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("You don't have access to this client", "error")
        return redirect(url_for('dashboard'))
    
    notifications = Notification.query.filter_by(client_id=client.id)\
                      .order_by(Notification.date_time.desc())\
                      .all()
    
    return render_template('notification_history.html', 
                         client=client,
                         notifications=notifications)
                         
@app.route('/client/<int:client_id>/visits')
@login_required
def visit_history(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("You don't have access to this client", "error")
        return redirect(url_for('dashboard'))
    
    visits = Visit.query.filter_by(client_id=client.id)\
              .order_by(Visit.date.desc())\
              .all()
              
    return render_template('visit_history.html', 
                         client=client,
                         visits=visits)

@app.route('/client/<int:client_id>')
@login_required
def client_detail(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("You don't have access to this client", "error")
        return redirect(url_for('dashboard'))
    
    medicines = Medicine.query.filter_by(client_id=client.id).all()
    med_times = MedTime.query.filter_by(client_id=client.id).all()
    reminders = Reminder.query.filter_by(client_id=client.id).all()
    visits = Visit.query.filter_by(client_id=client.id)\
              .order_by(Visit.date.desc(), Visit.time.desc())\
              .limit(3).all()
    notifications = Notification.query.filter_by(client_id=client.id)\
                       .order_by(Notification.date_time.desc())\
                       .limit(3).all()
    notification_count = Notification.query.filter_by(client_id=client.id).count()
    visit_count = Visit.query.filter_by(client_id=client.id).count()
              
    return render_template('client_detail.html', 
                         client=client,
                         visits=visits,
                         medicines=medicines,
                         med_times=med_times,
                         camera_url=camera_stream_url(client.camera_url),
                         reminders=reminders,
                         notifications=notifications,
                         notification_count=notification_count,
                         visit_count=visit_count)

@app.route('/client/<int:client_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_client(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        flash("Access denied", "error")
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        client.name = request.form.get('name', client.name)
        client.address = request.form.get('address', client.address)
        client.municipality = request.form.get('municipality', client.municipality)
        client.doctor_name = request.form.get('doctor_name', client.doctor_name)
        client.clinic_id = request.form.get('clinic_id', client.clinic_id)
        client.emergency_info = request.form.get('emergency_info', client.emergency_info)
        client.psychological_info = request.form.get('psychological_info', client.psychological_info)
        client.camera_url = request.form.get('camera_url', client.camera_url)
        client.device_name = request.form.get('device_name', client.device_name)  # NEW
        db.session.commit()
        flash("Client updated successfully", "success")
        return redirect(url_for('client_detail', client_id=client.id))
    
    return render_template('edit_client.html', client=client)

@app.route('/client/<int:client_id>/add_visit', methods=['GET', 'POST'])
@login_required
def add_visit(client_id):
    client = Client.query.get_or_404(client_id)
    if request.method == 'POST':
        visit = Visit(
            client_id=client.id,
            user_name=client.name,
            nurse_id=current_user.id,
            nurse_name=current_user.name,
            date=request.form['date'],
            time=request.form['time'],
            next_visit_date=request.form.get('next_visit_date'),
            notes=request.form['notes']
        )
        db.session.add(visit)
        db.session.commit()
        flash("Visit recorded successfully", "success")
        return redirect(url_for('client_detail', client_id=client.id))
    
    now = datetime.now()
    return render_template('add_visit.html', client=client, now=now)

# Get medicines for a client
@app.route('/client/<int:client_id>/medicines')
@login_required
def get_client_medicines(client_id):
    """Get medicines for a client - ensure this endpoint works"""
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    medicines = Medicine.query.filter_by(client_id=client.id).all()
    print(f"📋 Serving {len(medicines)} medicines for client {client_id}")
    
    return jsonify({
        'medicines': [{
            'id': med.id,
            'name': med.name,
            'side_effect': med.side_effect,
            'active': med.active,
            'schedule_type': med.schedule_type,
            'days': med.get_days_list(),
            'times': med.get_times_list()
        } for med in medicines]
    })


def notify_client_medicine_update(client_id):
    """Notify a client that their medicines have been updated"""
    client = Client.query.get(client_id)
    if client and client.cpr:
        message_queues[client.cpr].append({
            'type': 'medicine_updated',
            'message': 'Medicine list has been updated',
            'timestamp': datetime.utcnow().isoformat(),
            'client_id': client.id
        })
        app.logger.info(f"📢 Notified client {client.cpr} about medicine update")
        
        # Also send via WebSocket for real-time updates
        socketio.emit('medicine_updated', {
            'client_cpr': client.cpr,
            'message': 'Medicine list updated',
            'timestamp': datetime.utcnow().isoformat()
        })

# Update all medicine endpoints to trigger notifications
@app.route('/medicine/<int:medicine_id>', methods=['PUT'])
@login_required
def update_medicine(medicine_id):
    medicine = Medicine.query.get_or_404(medicine_id)
    client = Client.query.get(medicine.client_id)
    
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    data = request.json
    medicine.name = data.get('name', medicine.name)
    medicine.side_effect = data.get('side_effect', medicine.side_effect)
    medicine.active = data.get('active', medicine.active)
    medicine.schedule_type = data.get('schedule_type', medicine.schedule_type)
    
    # Update schedule fields
    medicine.set_days_list(data.get('days', []))
    medicine.set_times_list(data.get('times', []))
    
    db.session.commit()
    
    # 🔥 NOTIFY CLIENT IMMEDIATELY
    notify_client_medicine_update(client.id)
    
    return jsonify({'status': 'success'})

@app.route('/medicine/<int:medicine_id>', methods=['DELETE'])
@login_required
def delete_medicine(medicine_id):
    medicine = Medicine.query.get_or_404(medicine_id)
    client = Client.query.get(medicine.client_id)
    
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    db.session.delete(medicine)
    db.session.commit()
    
    # 🔥 NOTIFY CLIENT IMMEDIATELY
    notify_client_medicine_update(client.id)
    
    return jsonify({'status': 'success'})

@app.route('/client/<int:client_id>/medicines', methods=['POST'])
@login_required
def add_client_medicine(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    data = request.json
    medicine = Medicine(
        client_id=client.id,
        name=data['name'],
        side_effect=data.get('side_effect', ''),
        active=data.get('active', True),
        schedule_type=data.get('schedule_type', 'daily')
    )
    
    # Set schedule fields
    medicine.set_days_list(data.get('days', []))
    medicine.set_times_list(data.get('times', []))
    
    db.session.add(medicine)
    db.session.commit()
    
    # 🔥 NOTIFY CLIENT IMMEDIATELY
    notify_client_medicine_update(client.id)
    
    return jsonify({
        'status': 'success',
        'medicine': {
            'id': medicine.id,
            'name': medicine.name,
            'side_effect': medicine.side_effect,
            'active': medicine.active,
            'schedule_type': medicine.schedule_type,
            'days': medicine.get_days_list(),
            'times': medicine.get_times_list()
        }
    })

# Get visits for a client
@app.route('/client/<int:client_id>/visits')
@login_required
def get_client_visits(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    visits = Visit.query.filter_by(client_id=client.id).order_by(Visit.date.desc()).all()
    return jsonify({
        'visits': [{
            'id': visit.id,
            'nurse_name': visit.nurse_name,
            'date': visit.date,
            'time': visit.time,
            'next_visit_date': visit.next_visit_date,
            'notes': visit.notes
        } for visit in visits]
    })

# Add visit from client
@app.route('/client/<int:client_id>/visits', methods=['POST'])
@login_required
def add_client_visit(client_id):
    client = Client.query.get_or_404(client_id)
    if client.municipality != current_user.municipality and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    data = request.json
    visit = Visit(
        client_id=client.id,
        user_name=client.name,
        nurse_id=current_user.id,
        nurse_name=current_user.name,
        date=data['date'],
        time=data['time'],
        next_visit_date=data.get('next_visit_date'),
        notes=data.get('notes', '')
    )
    
    db.session.add(visit)
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'visit': {
            'id': visit.id,
            'nurse_name': visit.nurse_name,
            'date': visit.date,
            'time': visit.time,
            'next_visit_date': visit.next_visit_date,
            'notes': visit.notes
        }
    })

@app.route('/visits')
@login_required
def view_visits():
    visits = Visit.query.filter_by(nurse_id=current_user.id).order_by(Visit.date.desc()).all()
    return render_template('visits.html', visits=visits)

@app.route('/')
@login_required
def dashboard():
    # Clean up old heartbeats first
    cleanup_old_heartbeats()
    
    # Get filter parameter
    municipality_filter = request.args.get('municipality', '')
    
    # Base query
    if getattr(current_user, 'is_admin', False) or getattr(current_user, 'municipality', '') == 'all':
        if municipality_filter:
            clients = Client.query.filter_by(municipality=municipality_filter).order_by(Client.name.asc()).all()
        else:
            clients = Client.query.order_by(Client.name.asc()).all()
    else:
        clients = Client.query.filter_by(municipality=current_user.municipality).order_by(Client.name.asc()).all()

    # Get unique municipalities for admin filter
    municipalities = []
    if current_user.is_admin or current_user.municipality == 'all':
        municipalities = db.session.query(Client.municipality).distinct().all()
        municipalities = [m[0] for m in municipalities if m[0]]

    for client in clients:
        client.unread_notifications = Notification.query.filter(
            Notification.client_id == client.id,
            Notification.hidden == False
        ).order_by(Notification.date_time.desc()).all()
        
        # Add online status - client is online if they've sent a heartbeat in the last 2 minutes
        client.is_online = client.cpr in online_clients

    return render_template('dashboard.html', 
                         clients=clients, 
                         municipalities=municipalities,
                         current_filter=municipality_filter)

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        if not check_password_hash(current_user.password, current_password):
            flash('Current password is incorrect', 'error')
        elif new_password != confirm_password:
            flash('New passwords do not match', 'error')
        elif len(new_password) < 8:
            flash('Password must be at least 8 characters', 'error')
        else:
            current_user.password = generate_password_hash(new_password)
            db.session.commit()
            flash('Password changed successfully', 'success')
            return redirect(url_for('dashboard'))
    
    return render_template('change_password.html')

def init_db():
    with app.app_context():
        db.create_all()
        if not Nurse.query.filter_by(is_admin=True).first():
            admin = Nurse(
                cpr='admin',
                name='Admin',
                phone_number='12345678',
                municipality='all',
                password=generate_password_hash('admin'),
                is_admin=True
            )
            db.session.add(admin)
            db.session.commit()
            print("Created default admin user: CPR=admin, Password=admin")

def create_app():
    return app

# Initialize Flask-Migrate
migrate = Migrate(app, db)

def _start_heartbeat_cleanup_thread():
    def _loop():
        while True:
            try:
                cleanup_old_heartbeats()
            except Exception as e:
                app.logger.error(f"Heartbeat cleanup error: {e}")
            time.sleep(30)  # run every 30s
    t = threading.Thread(target=_loop, daemon=True)
    t.start()

_start_heartbeat_cleanup_thread()


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
