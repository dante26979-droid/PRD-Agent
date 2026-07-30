import { AgentTaskWorkbench } from "@/components/agent-task-workbench";

export default async function TaskPage({
  params,
}: {
  params: Promise<{ taskId: string }>;
}) {
  const { taskId } = await params;
  return <AgentTaskWorkbench taskId={taskId} />;
}
