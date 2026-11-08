import base64,datetime,hashlib,http.cookiejar,json,tempfile,threading,unittest,urllib.request,urllib.error
from unittest.mock import patch
from pathlib import Path
import app
P=dict(segment='prof',employees=8,sites=1,seats=8,tier='1',budget=6000,current_spend=6500,needs=['internet','email','security','backup'],city='Pune',fibre='available',has_gstin=False)
class DomainTests(unittest.TestCase):
 def test_full_price(self):
  q=app.quote(P,{'P01':1,'P19':1});self.assertEqual((q['total'],q['setup_total'],q['first_invoice']),(1885.64,2950,4835.64))
 def test_dependency(self):
  with self.assertRaisesRegex(ValueError,'needs one of'):app.quote(P,{'P09':1})
 def test_gst_cannot_be_self_attested_at_checkout(self):
  p=dict(P,has_gstin=True);self.assertTrue(app.quote(p,{'P03':1},provisional=True)['gst_pending'])
  with self.assertRaises(ValueError):app.quote(p,{'P03':1})
 def test_quantities(self):
  for n in [0,-1,1.5,True,1001]:
   with self.assertRaises(ValueError):app.quote(P,{'P01':n})
 def test_location_coverage(self):
  with self.assertRaisesRegex(ValueError,'locations'):app.quote(dict(P,sites=3),{'P02':1})
 def test_phase_two_budget(self):
  r=app.recommend(dict(P,budget=2000));self.assertLessEqual(r['quote']['total'],2000);self.assertTrue(r['later'])
 def test_lower_bandwidth_can_respect_tighter_budget(self):
  r=app.recommend(dict(P,budget=1400));self.assertIn('P01',r['items']);self.assertLessEqual(r['quote']['total'],1400)
 def test_budget_gap_visible(self):self.assertGreater(app.recommend(dict(P,budget=500))['quote']['budget_gap'],0)
 def test_wireless_backup_dependency(self):
  r=app.recommend(dict(P,fibre='unavailable'));self.assertIn('P04',r['items']);self.assertNotIn('P09',r['items'])
 def test_email_only(self):self.assertEqual(app.recommend(dict(P,needs=['email']))['items'],{'P16':8})
 def test_city_tier(self):
  with self.assertRaises(ValueError):app.quote(dict(P,tier='2'),{'P03':1},True)
 def test_chat_slots(self):
  p=app.parse_message('I run a clinic with 8 staff in Pune. Need email and internet. Budget ₹6,000.',{});self.assertEqual((p['segment'],p['employees'],p['budget']),('prof',8,6000))
 def test_gst_checksum(self):self.assertTrue(app.valid_gstin('19EOGXH4115Y1ZL'));self.assertFalse(app.valid_gstin('19EOGXH4115Y1Z0'))
 def test_data_funnel(self):
  a=app.analytics();self.assertEqual([s['count'] for s in a['funnel']],[5000,3406,1974,861,588,368]);self.assertEqual(a['churn'],15.2)
 def test_stale_address(self):
  d=app.extract_document([{'text':t,'confidence':.9} for t in ['Electricity Bill','Billed to','Demo Clinic','Bill date','2025-01-01']],'address');self.assertTrue(any(c['status']=='error' for c in d['checks']))
 def test_wrong_type(self):
  d=app.extract_document([{'text':'GST Registration Certificate','confidence':1}],'signatory');self.assertTrue(any(c['status']=='error' for c in d['checks']))
 def test_business_name_comparison(self):
  self.assertTrue(app.names_match('Joseph Shah (Prop. Harmony Dental Care)','Harmony Dental Care'))
  self.assertFalse(app.names_match('Saffron Dental Care LLP','Harmony Dental Care'))
 def test_postcode_mismatch(self):
  self.assertFalse(app.addresses_match('Unit 1, Pune - 411001','Unit 1, Pune - 411002'))
 def test_cancelled_specimen_is_not_approved(self):
  if not (app.ROOT/'tools/ocr').exists():self.skipTest('OCR unavailable')
  lines,_=app.read_document(app.DATA/'kyc_documents/K01_gst_certificate.png')
  d=app.extract_document(lines,'gst');self.assertTrue(any(c['status']=='error' and 'Cancelled' in c['title'] for c in d['checks']))
 def test_real_ocr(self):
  if not (app.ROOT/'tools/ocr').exists():self.skipTest('OCR unavailable')
  s=app.empty_session();s['registry']={'kind':'gst','name':app.GST['19EOGXH4115Y1ZL']['legal_name'],'status':'Active','identifier_hash':hashlib.sha256(b'19EOGXH4115Y1ZL').hexdigest()}
  for kind,suffix in [('gst','gst_certificate'),('address','address_proof'),('signatory','signatory_id')]:
   lines,_=app.read_document(app.DATA/'kyc_documents'/f'K02_{suffix}.png');s['documents'][kind]=app.extract_document(lines,kind)
  self.assertEqual(app.kyc_summary(s)['state'],'ready',app.kyc_summary(s))
class HTTPTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.old=app.DB_PATH;app.DB_PATH=Path(cls.tmp.name)/'test.sqlite3';app.init_db();cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);threading.Thread(target=cls.server.serve_forever,daemon=True).start();cls.base='http://127.0.0.1:'+str(cls.server.server_port)
 @classmethod
 def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();app.DB_PATH=cls.old;cls.tmp.cleanup()
 def client(self):return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
 def setUp(self):self.c=self.client();self.req('bootstrap')
 def req(self,path,body=None,c=None):
  r=urllib.request.Request(self.base+'/api/'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
  with (c or self.c).open(r) as response:return json.load(response)
 def test_ai_permission_is_separate_from_kyc(self):
  state=self.req('ai-consent',{'consent':True});self.assertTrue(state['ai_consent']);self.assertFalse(state['consent'])
  state=self.req('ai-consent',{'consent':False});self.assertFalse(state['ai_consent'])
 def test_no_ai_request_without_opt_in(self):
  with patch('gemini.discover',side_effect=AssertionError('No permission')):
   result=self.req('advisor',{'message':'I run a clinic with 8 staff. Need email. Budget 6000.'});self.assertTrue(result['ready'])
 def test_explicit_budget_survives_ai_slot_omission(self):
  self.req('ai-consent',{'consent':True})
  ai={'profile':dict(segment='prof',employees=8,sites=1,current_tools=['personal_email'],needs=['internet']), 'reply':'What is your budget?', 'mode':'gemini'}
  with patch('gemini.discover',return_value=ai):
   r=self.req('advisor',{'message':'My monthly budget including GST is 6000 rupees.'});self.assertEqual(r['profile']['budget'],6000);self.assertTrue(r['ready'])
 def test_negated_goal_is_not_reintroduced_by_literal_checks(self):
  self.req('ai-consent',{'consent':True})
  ai={'profile':dict(segment='prof',employees=8,sites=1,current_tools=['personal_email'],needs=['internet'],budget=6000), 'reply':'Captured.', 'mode':'gemini'}
  with patch('gemini.discover',return_value=ai):
   r=self.req('advisor',{'message':'We do not need email.'});self.assertNotIn('email',r['profile']['needs'])
 def test_consent_is_required(self):
  with self.assertRaises(urllib.error.HTTPError):self.req('lookup',{'kind':'gst','value':'19EOGXH4115Y1ZL'})
 def test_masking_and_isolation(self):
  self.req('consent',{'consent':True});s=self.req('lookup',{'kind':'gst','value':'19EOGXH4115Y1ZL'});self.assertNotIn('19EOGXH4115Y1ZL',json.dumps(s));self.assertIsNone(self.req('bootstrap',c=self.client())['state']['registry'])
 def test_idempotency_and_blocked_approval(self):
  self.req('recommend',{'profile':P});self.req('consent',{'consent':True});self.req('lookup',{'kind':'gst','value':'19EOGXH4115Y1ZL'});o=self.req('order',{'accept':True,'idempotency':'test'});self.assertEqual(o['id'],self.req('order',{'accept':True,'idempotency':'test'})['id'])
  with self.assertRaises(urllib.error.HTTPError):self.req('order/advance',{'id':o['id'],'note':'Reviewed every document.'})
  with self.assertRaises(urllib.error.HTTPError):self.req('order/'+o['id'],c=self.client())
 def test_handoff(self):
  self.assertTrue(self.req('callback',{'topic':'Help with my setup'})['id'].startswith('HELP-'));self.assertEqual(len(self.req('analytics')['callbacks']),1)
 def test_withdraw(self):
  self.req('consent',{'consent':True});self.req('callback',{'topic':'Help with test'});self.req('withdraw',{});self.assertFalse(self.req('bootstrap')['state']['consent']);self.assertEqual(self.req('analytics')['callbacks'],[])
 def test_complete_uploaded_document_journey(self):
  if not (app.ROOT/'tools/ocr').exists():self.skipTest('OCR unavailable')
  self.req('recommend',{'profile':P});self.req('consent',{'consent':True});self.req('lookup',{'kind':'gst','value':'19EOGXH4115Y1ZL'})
  for kind,suffix in [('gst','gst_certificate'),('address','address_proof'),('signatory','signatory_id')]:
   raw=(app.DATA/'kyc_documents'/f'K02_{suffix}.png').read_bytes()
   state=self.req('document',{'kind':kind,'data':base64.b64encode(raw).decode()})
  self.assertEqual(state['kyc']['state'],'ready')
  order=self.req('order',{'accept':True,'idempotency':'complete'})
  for target,extra in [('verified',{}),('scheduled',{'date':datetime.date.today().isoformat()}),('active',{})]:
   order=self.req('order/advance',dict(id=order['id'],note='Simulated review and provider completion.',**extra));self.assertEqual(order['status'],target)
  self.assertEqual(len(self.req('order/'+order['id'])['events']),5)
 def test_revoked_consent_blocks_progress(self):
  self.req('recommend',{'profile':P});self.req('consent',{'consent':True});self.req('lookup',{'kind':'gst','value':'19EOGXH4115Y1ZL'});o=self.req('order',{'accept':True});self.req('consent',{'consent':False})
  with self.assertRaises(urllib.error.HTTPError):self.req('order/advance',{'id':o['id'],'note':'Must not approve without consent.'})
 def test_expired_session_is_cleaned(self):
  self.req('callback',{'topic':'Expired session test'})
  with app.connect() as c:c.execute('UPDATE sessions SET updated=?',('2020-01-01T00:00:00+00:00',))
  self.assertEqual(self.req('analytics')['callbacks'],[])
 def test_foreign_origin(self):
  r=urllib.request.Request(self.base+'/api/consent',data=b'{"consent":true}',headers={'Content-Type':'application/json','Origin':'http://foreign.example'})
  with self.assertRaises(urllib.error.HTTPError) as e:self.c.open(r)
  self.assertEqual(e.exception.code,403)
if __name__=='__main__':unittest.main(verbosity=2)
