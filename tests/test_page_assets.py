import base64
import hashlib
from pathlib import Path
import re
import tempfile
import unittest
from channel_service.page_assets import PageAssets,policy_with_hashes

class PageAssetTests(unittest.TestCase):
 def test_html_is_self_contained_with_exact_hashes(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   (root/'app.js').write_text("document.title='ready';")
   (root/'style.css').write_text('body{color:red}')
   (root/'index.html').write_text('<link rel="stylesheet" href="/static/style.css"><div id="app"></div><script type="module" src="/static/app.js"></script>')
   html,hashes=PageAssets(root).render('index.html')
   self.assertNotIn('src="/static/app.js"',html);self.assertNotIn('rel="stylesheet"',html)
   script=re.search(r'<script[^>]*>(.*?)</script>',html,re.S)[1]
   digest="'sha256-"+base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()+"'"
   self.assertIn(digest,hashes['script-src'])
   policy=policy_with_hashes("default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'",hashes)
   self.assertIn(digest,policy);self.assertNotIn('unsafe-inline',policy);self.assertIn("frame-ancestors 'none'",policy)
 def test_asset_changes_invalidate_cached_page(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'index.html').write_text('<script src="/static/app.js"></script>');asset=root/'app.js';asset.write_text('first()')
   renderer=PageAssets(root);before=renderer.render('index.html')
   asset.write_text('secondVersion()');after=renderer.render('index.html')
   self.assertNotEqual(before,after);self.assertIn('secondVersion()',after[0])
 def test_external_and_traversing_assets_are_not_embedded(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);original='<script src="https://example.invalid/app.js"></script>'
   (root/'index.html').write_text(original);renderer=PageAssets(root)
   self.assertIsNone(renderer.asset('/static/../../private.js'));self.assertEqual(renderer.render('index.html')[0],original)
 def test_local_bundle_substitution(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'app.js').write_text("import './dependency.js'");(root/'app.bundle.mjs').write_text('bundled()')
   (root/'index.html').write_text('<script type="module" src="/static/app.js"></script>')
   html,_=PageAssets(root,{'app.js':'app.bundle.mjs'}).render('index.html');self.assertIn('bundled()',html);self.assertNotIn('dependency.js',html)
