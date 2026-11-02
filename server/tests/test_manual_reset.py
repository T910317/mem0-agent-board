"""手填重置是獨立提醒；不覆蓋官方窗口，壞值不得連帶改門檻。"""
import importlib.util
from pathlib import Path
import tempfile
import time

spec=importlib.util.spec_from_file_location('usage',Path(__file__).resolve().parents[1]/'usage.py')
U=importlib.util.module_from_spec(spec);spec.loader.exec_module(U)
U._conn(str(Path(tempfile.mkdtemp(prefix='manual-reset-'))/'usage.db'))
now=time.time();expected=U._iso(now+86400)
U.put({'providers':[{'id':'codex','windows':[{'key':'weekly','used':50,'resets_at':now+7*86400,'minutes':10080}]}]},now=now)
official=U.view(now=now)['providers'][0]['windows'][0]['resets_at']
result=U.settings_set({'codex_manual_reset':expected},now=now)
assert result['ok'] and U.settings_get()['codex_manual_reset']==expected
assert U.view(now=now)['providers'][0]['windows'][0]['resets_at']==official
for i,value in enumerate((True,123,{},[], 'bad','2026-12-01T12:00:00',U._iso(now-1),U._iso(now+367*86400))):
    result=U.settings_set({'normal_floor':90,'codex_manual_reset':value},now=now+i+1)
    assert not result['ok'],value
    assert U.settings_get()['normal_floor']==30
    assert U.settings_get()['codex_manual_reset']==expected
assert U.settings_set({'codex_manual_reset':None},now=now+20)['ok']
assert U.settings_get().get('codex_manual_reset') is None
print('PASS manual reset save/clear, 8 bad values, atomic update, official window preserved')
