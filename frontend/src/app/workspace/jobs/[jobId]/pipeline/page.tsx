import { KanbanBoard } from "@/components/pipeline/KanbanBoard";

export default async function PipelinePage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;
  return <KanbanBoard jobId={jobId} />;
}
