import { Card, Empty } from "../components/ui";
import { PageHead } from "../components/bits";

/** Placeholder: replaced by the Now page (attention inbox, running work, machines, accounts, alerts). */
export function NowPage() {
  return (
    <>
      <PageHead title="Now" intro="What needs you, what is running, and the state of the fleet." />
      <Card><Empty title="Now is being built">This page replaces Overview and Live.</Empty></Card>
    </>
  );
}
