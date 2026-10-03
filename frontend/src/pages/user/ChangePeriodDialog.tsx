import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, unwrap, type Subscription } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { formatDay } from "../../lib/access";
import { endOfDay } from "./GrantAccessDialog";
import styles from "./UserPage.module.css";

const DAY_MS = 24 * 60 * 60 * 1000;
const EXTENSIONS = [30, 90, 365] as const;

/** Продлить (или сократить) доступ: новый последний день. Продление считается от текущего конца, а не от сегодня. */
export function ChangePeriodDialog({
  subscription,
  open,
  onOpenChange,
}: {
  subscription: Subscription;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState<number | "date">(30);
  const [untilDate, setUntilDate] = useState("");
  const currentEnd = new Date(subscription.valid_until);

  const change = useMutation({
    mutationFn: (validUntil: string) =>
      unwrap(
        api.PATCH("/api/v1/subscriptions/{subscription_id}", {
          params: { path: { subscription_id: subscription.id } },
          body: { valid_until: validUntil },
        }),
        { 422: "Конец доступа должен быть позже его начала." },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["vpn-user"] });
      void queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
      close(false);
    },
  });

  function close(next: boolean) {
    if (!next) change.reset();
    onOpenChange(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    change.mutate(
      choice === "date"
        ? endOfDay(untilDate)
        : new Date(currentEnd.getTime() + choice * DAY_MS).toISOString(),
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="Изменить срок доступа"
      description={`Сейчас доступ действует до ${formatDay(subscription.valid_until)}.`}
    >
      <form className={styles.form} onSubmit={submit}>
        <fieldset className={styles.choice}>
          <legend className={styles.label}>Новый срок</legend>
          {EXTENSIONS.map((days) => (
            <label key={days}>
              <input type="radio" name="extend" checked={choice === days} onChange={() => setChoice(days)} />
              до {formatDay(new Date(currentEnd.getTime() + days * DAY_MS).toISOString())}
            </label>
          ))}
          <label>
            <input
              type="radio"
              name="extend"
              checked={choice === "date"}
              onChange={() => setChoice("date")}
            />
            Другая дата
          </label>
          {choice === "date" && (
            <input
              type="date"
              aria-label="Последний день доступа"
              className={styles.date}
              required
              value={untilDate}
              onChange={(event) => setUntilDate(event.target.value)}
            />
          )}
        </fieldset>
        {change.isError && <Notice>{change.error.message}</Notice>}
        <DialogActions>
          <Button variant="quiet" onClick={() => close(false)}>
            Отмена
          </Button>
          <Button
            type="submit"
            disabled={choice === "date" && untilDate === ""}
            busy={change.isPending}
            busyLabel="Сохраняем…"
          >
            Сохранить срок
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
