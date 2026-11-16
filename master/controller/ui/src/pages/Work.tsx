import { Card, Empty } from "../components/ui";
import { PageHead } from "../components/bits";

/** Placeholder: replaced by the Work list (work items as chains). */
export function WorkPage() {
  return (
    <>
      <PageHead title="Work" intro="Work items as chains of attempts." />
      <Card><Empty title="Work is being built">This page replaces Tasks and Pipelines.</Empty></Card>
    </>
  );
}
