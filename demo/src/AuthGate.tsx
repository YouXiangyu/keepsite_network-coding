import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { flushSync } from 'react-dom';
import { ArrowLeft, ArrowRight, CheckCircle, CircleNotch, Stack } from '@phosphor-icons/react';
import {
  ApiError, cancelSessionRequests, errorMessage, forgotPassword, getSession, installSession,
  onSessionExpired, registerAccount, resendCode, resetPassword, signIn, verifyEmail,
  type AuthSession, type SignedInSession,
} from './api';
import { navigate, useLocationPath } from './navigation';

type Mode = 'login' | 'register' | 'verify-email' | 'forgot-password' | 'reset-password';
const modes: Mode[] = ['login', 'register', 'verify-email', 'forgot-password', 'reset-password'];
const titles: Record<Mode, string> = {
  login: '欢迎回来', register: '创建你的票夹', 'verify-email': '验证你的邮箱',
  'forgot-password': '找回密码', 'reset-password': '设置新密码',
};
const descriptions: Record<Mode, string> = {
  login: '登录后，继续票根喜欢的日子。', register: '从一张照片开始，留下属于你的记忆。',
  'verify-email': '输入邮件中的验证码，完成账户验证。',
  'forgot-password': '填写注册邮箱，我们会发送密码重置验证码。',
  'reset-password': '输入邮件验证码和新密码，重新开启你的票根。',
};
const anonymous: AuthSession = { authenticated: false, configured: false, providers: { google: false, email: false } };

export function Brand() {
  return <div className="brand"><span className="brand-mark"><Stack size={23} weight="light" /></span><span>keepsite</span></div>;
}

export default function AuthGate({ children }: { children: (session: SignedInSession) => ReactNode }) {
  const path = useLocationPath();
  const route = path.split('?')[0].slice(1) as Mode;
  const mode = modes.includes(route) ? route : 'login';
  const initialLocation = new URL(window.location.href);
  const googleFailure = useRef(initialLocation.searchParams.get('auth_error') === 'google_login_failed');
  initialLocation.searchParams.delete('auth_error');
  const initialPath = initialLocation.pathname + initialLocation.search;
  const returnTo = useRef(modes.includes(route) || initialPath === '/' ? '/tickets' : initialPath);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [checking, setChecking] = useState(true);
  const [checkAttempt, setCheckAttempt] = useState(0);
  const [serviceError, setServiceError] = useState(googleFailure.current ? 'Google 登录未完成，请重试。' : '');

  useEffect(() => {
    const location = new URL(window.location.href);
    if (location.searchParams.has('auth_error')) {
      location.searchParams.delete('auth_error');
      navigate(location.pathname + location.search + location.hash, true);
    }
  }, []);

  useEffect(() => onSessionExpired(({ reason, logoutUrl }) => {
    const clear = () => {
      setSession(previous => ({ ...(previous ?? anonymous), authenticated: false, user: undefined, csrfToken: undefined }));
      setChecking(false);
      setServiceError(reason === 'logout' ? '已退出登录。' : '登录状态已结束，请重新登录。');
      returnTo.current = '/tickets';
      navigate('/login', true);
    };
    if (reason === 'logout') {
      // Commit the anonymous document before a provider navigation can cache this page.
      flushSync(clear);
      if (logoutUrl) window.location.assign(logoutUrl);
    } else clear();
  }), []);

  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    setChecking(true);
    getSession(controller.signal).then(next => {
      if (!live) return;
      installSession(next);
      setSession(next);
      setServiceError(!next.authenticated && googleFailure.current ? 'Google 登录未完成，请重试。' : '');
      if (next.authenticated && (modes.includes(route) || path === '/')) navigate('/tickets', true);
    }).catch(error => {
      if (!live) return;
      installSession(null);
      setSession(anonymous);
      setServiceError(error instanceof ApiError && error.status === 401 ? '请登录后继续。' : '登录服务暂时不可用，请稍后重试。');
    }).finally(() => { if (live) setChecking(false); });
    return () => { live = false; controller.abort(); };
  }, [checkAttempt]);

  useEffect(() => () => { cancelSessionRequests(); }, []);

  useEffect(() => {
    if (!session?.authenticated) return;
    let live = true;
    let controller: AbortController | null = null;
    const refresh = async () => {
      if (controller || document.visibilityState === 'hidden') return;
      controller = new AbortController();
      try {
        const next = await getSession(controller.signal);
        if (!live) return;
        if (!next.authenticated || next.user?.id !== session.user?.id || next.csrfToken !== session.csrfToken) installSession(next);
        setSession(next);
        if (!next.authenticated) {
          setServiceError('登录状态已结束，请重新登录。');
          returnTo.current = '/tickets';
          navigate('/login', true);
        }
      } catch {
        // A network interruption is not evidence that a valid session ended.
        // Authenticated 401 responses are handled by the global session listener.
      } finally { controller = null; }
    };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      live = false;
      controller?.abort();
      window.clearInterval(timer);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [session?.authenticated, session?.user?.id, session?.csrfToken]);

  if (checking) return <main className="auth-loading"><Brand /><p role="status"><CircleNotch className="spinner" size={20} />正在确认登录状态…</p></main>;
  if (session?.authenticated) return <>{children(session as SignedInSession)}</>;

  return <AuthPage key="auth" mode={mode} session={session ?? anonymous} serviceError={serviceError}
    retry={() => setCheckAttempt(value => value + 1)} onSignedIn={next => {
      installSession(next);
      setSession(next);
      setServiceError('');
      navigate(returnTo.current, true);
    }} />;
}

function AuthPage({ mode, session, serviceError, retry, onSignedIn }: {
  mode: Mode; session: AuthSession; serviceError: string; retry: () => void;
  onSignedIn: (session: SignedInSession) => void;
}) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const emailInput = useRef<HTMLInputElement>(null);
  const hasPassword = mode === 'login' || mode === 'register' || mode === 'reset-password';
  const newPassword = mode === 'register' || mode === 'reset-password';
  const hasCode = mode === 'verify-email' || mode === 'reset-password';
  const emailAvailable = session.configured && session.providers.email;

  useEffect(() => {
    setError('');
    setPassword('');
    setCode('');
    document.title = `${titles[mode]} · keepsite`;
  }, [mode]);

  const move = (next: Mode) => {
    if (lock.current) return;
    setMessage('');
    navigate('/' + next);
  };
  const perform = async (operation: () => Promise<void>) => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError('');
    try { await operation(); }
    catch (failure) {
      if (failure instanceof DOMException && failure.name === 'AbortError') return;
      if (failure instanceof ApiError && failure.code === 'email_unverified') {
        setMessage('请先完成邮箱验证，再登录。');
        navigate('/verify-email');
      } else setError(errorMessage(failure));
    } finally { lock.current = false; setBusy(false); }
  };
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!emailAvailable) return;
    if (newPassword && (password.length < 8 || password.length > 128)) {
      setError('密码需为 8–128 个字符，不限制字符组合。');
      return;
    }
    void perform(async () => {
      const address = email.trim();
      if (mode === 'login') onSignedIn(await signIn(address, password));
      if (mode === 'register') {
        const result = await registerAccount(address, password);
        if (!result.verificationRequired) throw new Error('未能确认注册结果，请稍后重试。');
        setEmail(result.email);
        setMessage('验证邮件已发送，请检查收件箱。');
        navigate('/verify-email');
      }
      if (mode === 'verify-email') {
        const result = await verifyEmail(address, code);
        if (!result.verified) throw new Error('未能确认邮箱验证结果。');
        setMessage('邮箱验证成功，现在可以登录了。');
        navigate('/login');
      }
      if (mode === 'forgot-password') {
        await forgotPassword(address);
        setMessage('如果该邮箱已注册，重置验证码会发送至你的邮箱。');
        navigate('/reset-password');
      }
      if (mode === 'reset-password') {
        await resetPassword(address, code, password);
        setMessage('密码已更新，请使用新密码登录。');
        navigate('/login');
      }
    });
  };

  return <main className="auth-page">
    <div className="auth-story"><Brand /><div className="auth-story-copy"><span className="eyebrow">A PLACE FOR YOUR MEMORIES</span>
      <h2>把喜欢的日子<br />好好留下来。</h2><p>一张照片，一段故事。<br />让生活里的小小珍藏，有一个属于自己的位置。</p>
      <div className="memory-illustration" aria-hidden="true"><div /><div /><div /><span>Every moment matters.</span></div>
    </div><small>YOUR LIFE, COLLECTED.</small></div>
    <section className="auth-panel" aria-labelledby="auth-heading"><div className="auth-mobile-brand"><Brand /></div>
      <div className="auth-form-wrap"><span className="eyebrow">keepsite account</span><h1 id="auth-heading">{titles[mode]}</h1><p className="auth-description">{descriptions[mode]}</p>
        {message && <p className="auth-notice" role="status"><CheckCircle size={20} />{message}</p>}
        {serviceError && <p className="auth-service-message" role="status">{serviceError}</p>}
        {!session.configured && <div className="auth-unavailable" role="alert"><p>登录服务暂时不可用，请稍后重试。</p><button className="secondary-button" onClick={retry}>重新连接</button></div>}
        {(mode === 'login' || mode === 'register') && <>
          <button className="google-button" type="button" disabled={!session.configured || !session.providers.google || busy}
            onClick={() => { cancelSessionRequests(); window.location.assign('/api/auth/google'); }}>
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M21.6 12.2c0-.7-.1-1.4-.2-2.1H12v4h5.4a4.6 4.6 0 0 1-2 3V20h3.3c1.9-1.7 2.9-4.2 2.9-7.8ZM12 22c2.7 0 5-.9 6.7-2.4l-3.3-2.6c-.9.6-2 1-3.4 1-2.6 0-4.8-1.8-5.6-4.1H3v2.7A10 10 0 0 0 12 22ZM6.4 13.9a6 6 0 0 1 0-3.8V7.4H3a10 10 0 0 0 0 9.2l3.4-2.7ZM12 6c1.5 0 2.8.5 3.8 1.5l2.9-2.9A10 10 0 0 0 3 7.4l3.4 2.7C7.2 7.8 9.4 6 12 6Z" /></svg>
            {session.providers.google ? '使用 Google 继续' : 'Google 登录暂不可用'}
          </button><div className="auth-divider"><span>或使用邮箱</span></div>
        </>}
        <form onSubmit={submit} className="auth-form">
          <fieldset disabled={!emailAvailable || busy}>
            <label htmlFor="auth-email">邮箱地址</label><input id="auth-email" ref={emailInput} type="email" name="email" autoComplete="email" required maxLength={254} value={email} onChange={event => setEmail(event.target.value)} placeholder="you@example.com" />
            {hasCode && <><label htmlFor="auth-code">邮件验证码</label><input id="auth-code" name="code" autoComplete="one-time-code" required value={code} onChange={event => setCode(event.target.value)} placeholder="输入邮件中的验证码" /></>}
            {hasPassword && <><label htmlFor="auth-password">{newPassword ? '设置密码' : '密码'}</label><input id="auth-password" name="password" type="password" autoComplete={newPassword ? 'new-password' : 'current-password'} required minLength={newPassword ? 8 : 1} maxLength={128} value={password} onChange={event => setPassword(event.target.value)} aria-describedby={newPassword ? 'password-help' : undefined} />
              {newPassword && <p className="field-hint" id="password-help">8–128 个字符，不限制字符组合。</p>}</>}
            {error && <p className="form-error" role="alert">{error}</p>}
            {mode === 'login' && <button className="auth-text-button forgot-link" type="button" onClick={() => move('forgot-password')}>忘记密码？</button>}
            <button className="primary-button auth-submit" type="submit">{busy ? <><CircleNotch className="spinner" size={19} />请稍候…</> : <>{mode === 'login' ? '登录' : mode === 'register' ? '创建账户' : mode === 'verify-email' ? '验证邮箱' : mode === 'forgot-password' ? '发送重置验证码' : '更新密码'}<ArrowRight size={19} /></>}</button>
          </fieldset>
        </form>
        {session.configured && !session.providers.email && <p className="field-hint" role="status">邮箱登录暂不可用，请稍后重试。</p>}
        {mode === 'verify-email' && <button className="auth-text-button" disabled={busy || !emailAvailable} onClick={() => {
          if (!emailInput.current?.reportValidity()) return;
          void perform(async () => { await resendCode(email.trim()); setMessage('验证码已重新发送，请检查收件箱。'); });
        }}>重新发送验证码</button>}
        <div className="auth-switch">{mode === 'login' ? <>还没有账户？<button disabled={busy} onClick={() => move('register')}>注册账户</button></> : mode === 'register' ? <>已经有账户？<button disabled={busy} onClick={() => move('login')}>登录</button></> : <button disabled={busy} onClick={() => move('login')}><ArrowLeft size={16} />返回登录</button>}</div>
        <p className="auth-footer">登录后，你的票根只在自己的账户中保存与管理。</p>
      </div>
    </section>
  </main>;
}
