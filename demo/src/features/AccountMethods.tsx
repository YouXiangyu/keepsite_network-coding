import { useEffect, useRef, useState, type FormEvent } from 'react';
import { addAccountPassword, errorMessage, getAccountMethods, resendCode, startAccountGoogle, type AccountMethods as Methods } from '../api';

export default function AccountMethods({ emailEnabled, googleEnabled }: { emailEnabled: boolean; googleEnabled: boolean }) {
  const [methods, setMethods] = useState<Methods | null>(null);
  const [mode, setMode] = useState<'register' | 'confirm' | 'link'>('register');
  const [email, setEmail] = useState('');
  const [emailLocked, setEmailLocked] = useState(false);
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState(() => {
    const result = new URL(window.location.href).searchParams.get('account_link');
    return result === 'success' ? 'Google 登录方式已绑定。' : result === 'failed' ? 'Google 绑定未完成，请重试。' : '';
  });
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    let mounted = true;
    const controller = new AbortController();
    getAccountMethods(controller.signal).then(value => { if (mounted) setMethods(value); }).catch(error => { if (mounted) setError(errorMessage(error)); });
    return () => { mounted = false; live.current = false; controller.abort(); };
  }, []);
  const reload = async () => {
    setBusy(true); setError('');
    try { const value = await getAccountMethods(); if (live.current) setMethods(value); }
    catch (error) { if (live.current) setError(errorMessage(error)); }
    finally { if (live.current) setBusy(false); }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try {
      const result = await addAccountPassword(mode, email, password, code);
      if (!live.current) return;
      setPassword('');
      if ('verificationRequired' in result) { setEmail(result.email); setEmailLocked(true); setMode('confirm'); setNotice('请查收验证邮件，再输入验证码和刚设置的密码。'); }
      else { setMethods(result); setCode(''); setNotice('邮箱密码登录方式已添加，可使用任一已绑定方式登录这个票夹。'); }
    } catch (error) { if (live.current) setError(errorMessage(error)); }
    finally { if (live.current) setBusy(false); }
  };
  const google = async () => {
    setBusy(true); setError('');
    try { const url = await startAccountGoogle(); if (live.current) window.location.assign(url); }
    catch (error) { if (live.current) { setError(errorMessage(error)); setBusy(false); } }
  };
  const resend = async () => {
    setBusy(true); setError(''); setNotice('');
    try { await resendCode(email); if (live.current) setNotice('验证码请求已提交，请查收邮件。'); }
    catch (error) { if (live.current) setError(errorMessage(error)); }
    finally { if (live.current) setBusy(false); }
  };
  return <section className="status-panel account-methods" aria-labelledby="account-methods-title" aria-busy={busy}>
    <h2 id="account-methods-title">登录方式</h2><p>绑定后，邮箱密码和 Google 都能独立登录当前票夹。其他票夹的登录方式不能合并到此账号。</p>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {!methods ? <button className="secondary-button" disabled={busy} onClick={() => void reload()}>重新读取登录方式</button> : <>
      <p>Google：{methods.google.linked ? `已绑定 · ${methods.google.email}` : '未绑定'}</p>
      {!methods.google.linked && methods.password.linked && <p>请绑定使用另一个邮箱的 Google 账号。</p>}
      {!methods.google.linked && googleEnabled && <button className="secondary-button" disabled={busy} onClick={() => void google()}>{busy ? '正在处理…' : '绑定 Google'}</button>}
      <p>邮箱密码：{methods.password.linked ? `已添加 · ${methods.password.email}` : '未添加'}</p>
      {!methods.password.linked && methods.google.linked && <p>Google 先登录时，请使用另一个可收验证码的邮箱设置密码。</p>}
      {!methods.password.linked && emailEnabled && <form className="account-method-form" onSubmit={event => void submit(event)}>
        <label>邮箱<input type="email" autoComplete="email" required maxLength={254} value={email} disabled={busy || emailLocked} onChange={event => setEmail(event.target.value)} /></label>
        {mode === 'confirm' && <label>邮件验证码<input autoComplete="one-time-code" required value={code} disabled={busy} onChange={event => setCode(event.target.value)} /></label>}
        <label>{mode === 'link' ? '已有邮箱密码' : mode === 'confirm' ? '刚设置的密码' : '设置密码'}<input type="password" autoComplete={mode === 'link' ? 'current-password' : 'new-password'} required minLength={12} maxLength={128} aria-describedby="account-password-policy" value={password} disabled={busy} onChange={event => setPassword(event.target.value)} /></label>
        <small id="account-password-policy">密码需为 12–128 个字符，包含大小写字母、数字和符号。</small>
        <div className="action-row"><button className="primary-button" disabled={busy}>{busy ? '正在处理…' : mode === 'register' ? '发送验证邮件' : mode === 'confirm' ? '验证并添加' : '验证已有邮箱并绑定'}</button>
        <button type="button" className="text-button" disabled={busy} onClick={() => { setMode(mode === 'link' ? 'register' : 'link'); setEmailLocked(false); setPassword(''); setCode(''); setError(''); setNotice(''); }}>{mode === 'link' ? '使用新邮箱' : '绑定已有邮箱密码'}</button>
        {mode !== 'confirm' && <button type="button" className="text-button" disabled={busy} onClick={() => { setMode('confirm'); setEmailLocked(false); setPassword(''); setError(''); setNotice('输入此前设置的邮箱、密码和邮件验证码。'); }}>已有邮件验证码</button>}</div>
        {mode === 'confirm' && <button type="button" className="text-button" disabled={busy || !email} onClick={() => void resend()}>重新发送邮件验证码</button>}
      </form>}
    </>}
  </section>;
}
