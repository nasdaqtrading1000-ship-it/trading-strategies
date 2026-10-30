"""Offline integration checks; no production database, Telegram or broker calls."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

from strategy_names import STRATEGY_BRANDS, strategy_display_name, strategy_internal_name, strategy_labels, strategy_public_filename, strategy_internal_filename, strategy_public_text


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self,tag,attrs):
        attributes = dict(attrs)
        if tag in ('script','style'):
            self.hidden += 1
        if tag=='input' and attributes.get('type') not in ('hidden','password','checkbox'):
            self.parts.append(attributes.get('value',''))

    def handle_endtag(self,tag):
        if tag in ('script','style'):
            self.hidden = max(0,self.hidden-1)

    def handle_data(self,value):
        if not self.hidden:
            self.parts.append(value)


class StrategyBrandTests(unittest.TestCase):
    def test_aliases_keep_technical_identity_and_payload_unchanged(self):
        for old, txt, brand in STRATEGY_BRANDS:
            self.assertEqual(strategy_display_name(old), brand)
            self.assertEqual(strategy_display_name(txt[:-4]), brand)
            self.assertEqual(strategy_display_name(brand), brand)
            self.assertEqual(strategy_display_name(brand.split(' (')[0]), brand)
            self.assertEqual(strategy_internal_name(brand.split(' (')[0]), old)
            self.assertEqual(strategy_internal_name(brand), old)
            operation = {'strategy_name':old,'txt_name':txt,'operation_key':'stable-id','shares':1.25,'entry_price':123.4}
            branded = strategy_labels(operation)
            self.assertEqual(branded['strategy_name'], brand)
            self.assertEqual(operation['strategy_name'], old)
            for key in ('txt_name','operation_key','shares','entry_price'):
                self.assertEqual(branded[key], operation[key])
        self.assertEqual(strategy_display_name('Momentum Intradia'), 'Momentum Intradia')
        self.assertEqual(strategy_display_name('Una estrategia nueva'), 'Una estrategia nueva')

    def test_public_file_labels_and_embedded_messages(self):
        for old,txt,brand in STRATEGY_BRANDS:
            public_file = strategy_public_filename(txt)
            self.assertEqual(public_file,brand.split(' (')[0]+'.txt')
            self.assertEqual(strategy_internal_filename(public_file),txt)
            self.assertIn(brand,strategy_public_text('Aviso de '+old+' recibido.'))
        self.assertEqual(strategy_internal_filename('Everest.py'),'Momentum.py')
        self.assertEqual(strategy_internal_filename('Cho Oyu.py'),'AcumulaMetales.py')
        self.assertEqual(strategy_public_filename('otra.py'),'otra.py')

    def test_telegram_builders_offline(self):
        root = Path(os.environ.get('STRATEGY_ENGINE_ROOT', '.'))
        signal_path = root / 'Estrategias/send_telegram_signals.py'
        if not signal_path.exists():
            self.skipTest('Private engine is not included in the publication repository')
        functions = [
            (signal_path, 'build_message', {'strategy_display_name':strategy_display_name}),
            (root / 'Estrategias/simulate_operations.py', 'build_telegram_operation_message', {
                'strategy_display_name':strategy_display_name,
                'float_value':lambda value: float(value or 0), 'format_short_date':lambda value: str(value or '')}),
        ]
        for path, name, scope in functions:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            fn = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            exec(compile(ast.Module(body=[fn], type_ignores=[]),str(path),'exec'), scope)
            for old,txt,brand in STRATEGY_BRANDS:
                if name=='build_message':
                    output = scope[name](Path(txt).stem, 'TEST | Precio: 100')
                    self.assertIn(brand, output)
                else:
                    for status in ('OPEN','CLOSED'):
                        op = {'strategy_name':old,'txt_name':txt,'symbol':'TEST','status':status}
                        output = scope[name](op)
                        self.assertIn(brand,output)
                        self.assertEqual(op['strategy_name'],old)

    def test_replicator_visible_text_hides_internal_names(self):
        from test_buying_power import app as replicator
        row = {'strategy_name':'Momentum','message':'Conflicto con Acumulacion','profit_value':0}
        config = dict(replicator.DEFAULT_CONFIG,selected_txt_names=['Momentum.txt'])
        with patch.object(replicator,'load_config',return_value=config), \
             patch.object(replicator,'available_strategies',return_value=[{'name':old,'signals_txt_name':txt} for old,txt,brand in STRATEGY_BRANDS]), \
             patch.object(replicator,'operation_status_rows',return_value=[row]), \
             patch.object(replicator,'replication_rows',return_value=[row]), \
             patch.object(replicator,'replication_view_revision',return_value='offline'):
            response = replicator.app.test_client().get('/')
        self.assertEqual(response.status_code,200)
        html = response.get_data(as_text=True)
        visible = VisibleText()
        visible.feed(html)
        content = ' '.join(visible.parts)
        for old,txt,brand in STRATEGY_BRANDS:
            self.assertIn(brand,content)
            self.assertNotIn(old,content)
            self.assertNotIn(txt,content)
        self.assertIn('value="Momentum.txt"',html)


class WebBrandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.engine = create_engine('sqlite:///'+str(Path(cls.tmp.name)/'test.db'))
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.addClassCleanup(cls.engine.dispose)
        # Existing installations already have this table before legacy migrations.
        with cls.engine.begin() as conn:
            conn.execute(text("CREATE TABLE execution_status (task_key TEXT PRIMARY KEY,label TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'IDLE',last_started_at TIMESTAMP,last_finished_at TIMESTAMP,last_returncode INTEGER,duration_seconds INTEGER NOT NULL DEFAULT 0,message TEXT NOT NULL DEFAULT '',updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"))
        import db
        # Redirect init_db to an isolated temporary database before module import.
        with patch.object(db,'engine',cls.engine), patch.object(db,'SQLITE_DATABASE',str(Path(cls.tmp.name)/'test.db')), patch.dict(os.environ,{'ENABLE_WEB_SCHEDULER':'0','SITE_PASSWORD':''}):
            spec = importlib.util.spec_from_file_location('web_brands_test',Path(__file__).parent/'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True)
        with cls.engine.begin() as conn:
            conn.execute(text('DELETE FROM strategies'))
            for old,txt,brand in STRATEGY_BRANDS:
                conn.execute(text("INSERT INTO strategies(name,signals_txt_name,risk_level,web_visible,replicator_available,public_visible) VALUES (:name,:txt,'Medio',1,1,1)"),{'name':old,'txt':txt})
            conn.execute(text("INSERT INTO users(id,email,password_hash,name,has_access,has_replicator_access) VALUES (1,'offline@example.test',:hash,'Everest',1,1)"),{'hash':generate_password_hash('test-password')})
            conn.execute(text("INSERT INTO simulated_operations(operation_key,strategy_name,txt_name,symbol,direction,status,opened_at,shares,entry_price) VALUES ('keep-same-key','Acumulacion','Acumulacion.txt','TEST','LONG','OPEN',CURRENT_TIMESTAMP,1.25,123.4)"))

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id']=1
            s['admin_logged_in']=True

    def test_desktop_mobile_admin_and_template_compilation(self):
        for name in self.app.jinja_env.list_templates():
            self.app.jinja_env.get_template(name)
        for path in ('/','/mobile','/admin'):
            response = self.client.get(path)
            self.assertEqual(response.status_code,200,path)
            html = response.get_data(as_text=True)
            for old,txt,brand in STRATEGY_BRANDS:
                self.assertIn(brand,html,path)
            self.assertNotIn('>Momentum<',html)
            visible = VisibleText()
            visible.feed(html)
            content = ' '.join(visible.parts)
            for old,txt,brand in STRATEGY_BRANDS:
                self.assertNotIn(old,content,path)
                self.assertNotIn(txt,content,path)

    def test_replicator_catalog_returns_brands_and_original_txt(self):
        login = self.client.post('/api/replicator/login',json={'email':'offline@example.test','password':'test-password'})
        self.assertEqual(login.status_code,200)
        response = self.client.get('/api/replicator/strategies',headers={'Authorization':'Bearer '+login.json['access_token']})
        self.assertEqual(response.status_code,200)
        catalog = {row['signals_txt_name']:row['name'] for row in response.json['strategies']}
        for old,txt,brand in STRATEGY_BRANDS:
            self.assertEqual(catalog[txt],brand)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT name FROM users WHERE id=1")).scalar_one(),'Everest')
            self.assertEqual(conn.execute(text("SELECT name FROM strategies WHERE signals_txt_name='Momentum.txt'")).scalar_one(),'Momentum')
        operations = self.client.post('/api/replicator/operations',headers={'Authorization':'Bearer '+login.json['access_token']},json={'selected_txt_names':['Acumulacion.txt']})
        self.assertEqual(operations.status_code,200)
        row = operations.json['operations'][0]
        self.assertEqual(row['strategy_name'],'Lhotse (ACU)')
        self.assertEqual(row['txt_name'],'Acumulacion.txt')
        self.assertEqual(row['operation_key'],'keep-same-key')
        self.assertEqual(row['shares'],1.25)
        self.assertEqual(row['entry_price'],123.4)

    def test_admin_saving_public_name_preserves_engine_name(self):
        with self.engine.connect() as conn:
            sid = conn.execute(text("SELECT id FROM strategies WHERE name='Momentum'")).scalar_one()
        response = self.client.post(f'/admin/strategies/{sid}/quick-update',data={'name':'Everest','risk_level':'Medio','signals_txt_name':'Everest.txt','python_file':'Everest.py','web_visible':'on','replicator_available':'on'})
        self.assertEqual(response.status_code,302)
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT name FROM strategies WHERE id=:id'),{'id':sid}).scalar_one(),'Momentum')
            self.assertEqual(conn.execute(text('SELECT python_file FROM strategies WHERE id=:id'),{'id':sid}).scalar_one(),'Momentum.py')


if __name__ == '__main__':
    unittest.main()
