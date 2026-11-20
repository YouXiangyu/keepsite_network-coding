import { useEffect, useState } from 'react';
import { ArrowLeft, DownloadSimple } from '@phosphor-icons/react';
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
  return <main className="android-download"><div className="download-card">
    <Brand />
    <span className="eyebrow">ANDROID APP</span>
    <h1>下载 Android App</h1>
    <p className="download-intro">在手机上录入和查看电影票，与网页端使用同一账号同步。</p>
    {error ? <div className="download-release"><p className="form-error" role="alert">{error}</p><button className="secondary-button" onClick={() => { setError(''); setAttempt(value => value + 1); }}>重试</button></div>
      : result === undefined ? <p className="status-note" role="status">正在查询最新正式版…</p>
      : result === null ? <p className="status-note" role="status">暂时无法下载：尚无正式版本。</p>
      : <div className="download-release">
        <h2>最新正式版 {result.version}</h2>
        {result.url ? <a className="primary-button wide" href={result.url}><DownloadSimple size={20} />下载最新版 APK</a> : <p className="status-note" role="status">暂时无法下载：此版本未提供 APK。</p>}
        <p className="field-hint">下载后在手机上打开 APK 完成安装；首次安装可能需要允许“安装未知应用”。</p>
        <details className="release-notes"><summary>版本说明</summary>
          <p>{result.notes.trim() || '此版本未提供版本说明。'}</p>
        </details>
      </div>}
    <a className="auth-text-button download-back" href="/login"><ArrowLeft size={16} />返回登录页</a>
  </div></main>;
}
