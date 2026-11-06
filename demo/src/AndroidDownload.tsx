import { useEffect, useState } from 'react';
import { Brand } from './AuthGate';

import { latestAndroidRelease, type AndroidRelease } from './api';

export default function AndroidDownload() {
  const [result, setResult] = useState<AndroidRelease | null>();
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    document.title = '下载 Android App · keepsite';
    const controller = new AbortController();
    latestAndroidRelease(controller.signal).then(value => {
      if (!controller.signal.aborted) setResult(value);
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof TypeError ? '无法连接 GitHub，请检查网络后重试。' : reason.message || '无法读取版本信息。');
    });
    return () => controller.abort();
  }, [attempt]);
  return <main className="android-download">
    <Brand /><h1>下载 Android App</h1>
    {error ? <><p role="alert">{error}</p><button className="secondary-button" onClick={() => { setError(''); setAttempt(value => value + 1); }}>重试</button></>
      : result === undefined ? <p role="status">正在查询最新正式版…</p>
      : result === null ? <p role="status">暂时无法下载：尚无正式版本。</p>
      : <><h2>最新正式版 {result.version}</h2>
        {result.url ? <a className="primary-button" href={result.url}>下载最新版 APK</a> : <p role="status">暂时无法下载：此版本未提供 APK。</p>}
        <h3>版本说明</h3><p className="release-notes">{result.notes.trim() || '此版本未提供版本说明。'}</p></>}
    <p><a href="/login">返回登录页</a></p>
  </main>;
}
