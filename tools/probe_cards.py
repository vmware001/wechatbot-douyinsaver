import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import douyin_saver as ds

cfg = ds.load_config()
browser = ds.Browser(cfg["debug_url"])
browser.ensure_connected()
chat = browser.chat

# 1) what cards_in_view sees
cards = browser.cards_in_view()
print("cards_in_view:", len(cards))
for c in cards[:10]:
    print("  card:", c.get("itemId"), c.get("author"), (c.get("title") or "")[:20])

# 2) raw DOM: does the selector match anything?
r = chat.js("""
(function(){
  var cards = document.querySelectorAll('""" + ds.CARD_SEL + """');
  var all = document.querySelectorAll('[class*=ShareAweme]');
  var msg = document.querySelectorAll('[class*=MessageItem]');
  var sc = document.querySelector('""" + ds.SCROLLER_SEL + """');
  return {
    card_sel: cards.length,
    share_aweme_any: all.length,
    msg_items: msg.length,
    scroller: !!sc,
    scroll_top: sc ? sc.scrollTop : null,
    scroller_children: sc ? sc.children.length : 0
  };
})()
""")
print("dom probe:", json.dumps(r, ensure_ascii=False))

# 3) sample of share-ish class names currently in the DOM
r2 = chat.js("""
(function(){
  var set = {};
  document.querySelectorAll('[class]').forEach(function(e){
    var cls = e.className;
    if (typeof cls !== 'string') return;
    cls.split(/\\s+/).forEach(function(c){
      if (/share|Share|aweme|Aweme/.test(c)) set[c] = (set[c]||0)+1;
    });
  });
  return Object.entries(set).sort(function(a,b){return b[1]-a[1]}).slice(0,15);
})()
""")
print("share-ish classes:", json.dumps(r2, ensure_ascii=False))
