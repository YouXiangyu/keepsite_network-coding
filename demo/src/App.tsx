import AuthGate from './AuthGate';
import Prototype from './Prototype';

import AndroidDownload from './AndroidDownload';
import { useLocationPath } from './navigation';

export default function App() {
  const path = useLocationPath();
  if (path.split('?')[0] === '/download/android') return <AndroidDownload />;
  return <AuthGate>{session => <Prototype key={session.user.id} session={session} />}</AuthGate>;
}
