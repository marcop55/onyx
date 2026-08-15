"use client";

import type { MattermostBot } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@opal/components";

interface MattermostBotTableProps {
  mattermostBots: MattermostBot[];
  onEdit?: (mattermostBot: MattermostBot) => void;
  onDelete?: (mattermostBot: MattermostBot) => void;
}

export function MattermostBotTable({
  mattermostBots,
  onEdit,
  onDelete,
}: MattermostBotTableProps) {
  const sortedBots = [...mattermostBots].sort((a, b) => a.id - b.id);

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Server</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Identity</TableHead>
          <TableHead>Health</TableHead>
          <TableHead>Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {sortedBots.map((mattermostBot) => (
          <TableRow key={mattermostBot.id}>
            <TableCell>{mattermostBot.name}</TableCell>
            <TableCell>{mattermostBot.url}</TableCell>
            <TableCell>
              {mattermostBot.enabled ? (
                <Badge variant="success">Enabled</Badge>
              ) : (
                <Badge variant="destructive">Disabled</Badge>
              )}
            </TableCell>
            <TableCell>{mattermostBot.bot_username}</TableCell>
            <TableCell>
              <Badge
                variant={
                  mattermostBot.health_status === "ok" ? "success" : "secondary"
                }
              >
                {mattermostBot.health_status}
              </Badge>
            </TableCell>
            <TableCell>
              <div className="flex gap-2">
                <Button onClick={() => onEdit?.(mattermostBot)} size="sm">
                  Edit
                </Button>
                <Button
                  onClick={() => onDelete?.(mattermostBot)}
                  size="sm"
                  variant="danger"
                >
                  Delete
                </Button>
              </div>
            </TableCell>
          </TableRow>
        ))}
        {sortedBots.length === 0 && (
          <TableRow>
            <TableCell
              colSpan={6}
              className="text-center text-muted-foreground"
            >
              Add a Mattermost bot to start handling Mattermost messages with
              Onyx.
            </TableCell>
          </TableRow>
        )}
      </TableBody>
    </Table>
  );
}
