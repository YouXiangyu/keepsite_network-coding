import AuthGate from './AuthGate';
import Prototype from './Prototype';

export default function App() {
  return <AuthGate>{session => <Prototype key={session.user.id} session={session} />}</AuthGate>;
}
