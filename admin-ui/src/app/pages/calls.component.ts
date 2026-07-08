import { CommonModule } from '@angular/common';
import { Component, Inject, OnInit, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import {
  MatDialog,
  MatDialogModule,
  MatDialogRef,
  MAT_DIALOG_DATA,
} from '@angular/material/dialog';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';

import {
  ApiService,
  Appointment,
  CallDetail,
  CallSummary,
  CallTurn,
} from '../api.service';

function formatDuration(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

@Component({
  selector: 'app-calls',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatCardModule,
    MatChipsModule,
    MatDialogModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatSnackBarModule,
    MatTableModule,
    MatTooltipModule,
  ],
  template: `
    <div class="page">
      <div class="page-header">
        <h1>Calls</h1>
        <button mat-stroked-button (click)="refresh()" matTooltip="Refresh">
          <mat-icon>refresh</mat-icon> Refresh
        </button>
      </div>

      @if (error()) {
        <div class="error-banner">{{ error() }}</div>
      }

      @if (loading()) {
        <mat-spinner diameter="40"></mat-spinner>
      } @else if (calls().length === 0) {
        <mat-card>
          <mat-card-content>
            No calls recorded yet. Once the bot answers a call, its transcript
            will show up here.
          </mat-card-content>
        </mat-card>
      } @else {
        <mat-card class="table-card">
          <table mat-table [dataSource]="calls()" class="mat-elevation-z0">
            <ng-container matColumnDef="started">
              <th mat-header-cell *matHeaderCellDef>When</th>
              <td mat-cell *matCellDef="let c">
                <div class="started">{{ formatDateTime(c.started_at) }}</div>
                <div class="session-id">
                  <mat-icon>tag</mat-icon>
                  {{ c.session_id }}
                </div>
              </td>
            </ng-container>

            <ng-container matColumnDef="duration">
              <th mat-header-cell *matHeaderCellDef>Duration</th>
              <td mat-cell *matCellDef="let c">
                {{ formatDuration(c.duration_seconds) }}
              </td>
            </ng-container>

            <ng-container matColumnDef="turns">
              <th mat-header-cell *matHeaderCellDef>Turns</th>
              <td mat-cell *matCellDef="let c">
                <div class="turns">
                  <span matTooltip="Caller turns">
                    <mat-icon>person</mat-icon> {{ c.user_turn_count }}
                  </span>
                  <span matTooltip="Total turns">
                    <mat-icon>chat</mat-icon> {{ c.turn_count }}
                  </span>
                </div>
              </td>
            </ng-container>

            <ng-container matColumnDef="outcome">
              <th mat-header-cell *matHeaderCellDef>Outcome</th>
              <td mat-cell *matCellDef="let c">
                @if (c.outcome === 'booked') {
                  <mat-chip class="outcome-booked">
                    <mat-icon>event_available</mat-icon>
                    Appointment booked
                  </mat-chip>
                } @else {
                  <mat-chip class="outcome-none">
                    <mat-icon>call_end</mat-icon>
                    No booking
                  </mat-chip>
                }
              </td>
            </ng-container>

            <ng-container matColumnDef="actions">
              <th mat-header-cell *matHeaderCellDef class="actions-col"></th>
              <td mat-cell *matCellDef="let c" class="actions-col">
                <button mat-stroked-button (click)="open(c)">
                  <mat-icon>subject</mat-icon>
                  Transcript
                </button>
              </td>
            </ng-container>

            <tr mat-header-row *matHeaderRowDef="displayed"></tr>
            <tr
              mat-row
              *matRowDef="let row; columns: displayed"
              class="clickable-row"
              (click)="open(row)"
            ></tr>
          </table>
        </mat-card>
      }
    </div>
  `,
  styles: [
    `
      .table-card {
        padding: 0;
      }
      table {
        width: 100%;
      }
      .started {
        font-weight: 500;
      }
      .session-id {
        display: flex;
        align-items: center;
        gap: 4px;
        font-family: 'JetBrains Mono', 'Consolas', monospace;
        font-size: 12px;
        color: #6b7280;
        mat-icon {
          font-size: 14px;
          width: 14px;
          height: 14px;
        }
      }
      .turns {
        display: flex;
        gap: 12px;
        color: #4b5563;
        font-size: 13px;
        span {
          display: inline-flex;
          align-items: center;
          gap: 4px;
        }
        mat-icon {
          font-size: 16px;
          width: 16px;
          height: 16px;
        }
      }
      .outcome-booked {
        background: #dcfce7 !important;
        color: #166534 !important;
        mat-icon {
          color: #16a34a;
          font-size: 16px;
          width: 16px;
          height: 16px;
          margin-right: 4px;
        }
      }
      .outcome-none {
        background: #f1f5f9 !important;
        color: #475569 !important;
        mat-icon {
          color: #64748b;
          font-size: 16px;
          width: 16px;
          height: 16px;
          margin-right: 4px;
        }
      }
      .actions-col {
        width: 140px;
        text-align: right;
      }
      .clickable-row {
        cursor: pointer;
      }
      .clickable-row:hover td {
        background: #f9fafb;
      }
    `,
  ],
})
export class CallsComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly dialog = inject(MatDialog);
  private readonly snack = inject(MatSnackBar);

  readonly displayed = ['started', 'duration', 'turns', 'outcome', 'actions'];
  readonly calls = signal<CallSummary[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly formatDuration = formatDuration;
  readonly formatDateTime = formatDateTime;

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.listCalls().subscribe({
      next: (c) => {
        this.calls.set(c);
        this.loading.set(false);
      },
      error: (e: Error) => {
        this.error.set(e.message);
        this.loading.set(false);
      },
    });
  }

  open(call: CallSummary): void {
    this.api.getCall(call.session_id).subscribe({
      next: (detail) => {
        this.dialog.open(CallDetailComponent, {
          data: detail,
          width: '720px',
          maxWidth: '96vw',
          maxHeight: '90vh',
        });
      },
      error: (e: Error) =>
        this.snack.open(e.message, 'Dismiss', { duration: 5000 }),
    });
  }
}

@Component({
  selector: 'app-call-detail',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatChipsModule,
    MatDialogModule,
    MatIconModule,
  ],
  template: `
    <h2 mat-dialog-title class="detail-title">
      <div>
        Call
        <span class="session-id">{{ data.session_id }}</span>
      </div>
      <div class="detail-meta">
        {{ formatDateTime(data.started_at) }}
        &nbsp;·&nbsp;
        {{ formatDuration(data.duration_seconds) }}
        &nbsp;·&nbsp;
        {{ data.turn_count }} turns
      </div>
    </h2>
    <mat-dialog-content class="detail-content">
      @if (data.appointments.length > 0) {
        <section class="outcome-section">
          <h3>Outcome</h3>
          @for (a of data.appointments; track a.id) {
            <div class="appt-card">
              <div class="appt-header">
                <mat-icon>event_available</mat-icon>
                <span class="appt-title">
                  {{ a.service }} with {{ a.stylist }}
                </span>
              </div>
              <div class="appt-body">
                <div>
                  <b>{{ a.customer_name }}</b>
                  <span class="muted">({{ a.customer_phone }})</span>
                </div>
                <div>
                  {{ a.date }} at {{ a.start_time }}<span *ngIf="a.end_time"
                    >–{{ a.end_time }}</span
                  >
                </div>
              </div>
              <div class="appt-actions">
                <button mat-stroked-button (click)="viewInCalendar(a)">
                  <mat-icon>event</mat-icon> View in calendar
                </button>
              </div>
            </div>
          }
        </section>
      } @else {
        <section class="outcome-section">
          <h3>Outcome</h3>
          <div class="no-outcome">
            <mat-icon>call_end</mat-icon>
            No appointment was booked during this call.
          </div>
        </section>
      }

      <section class="transcript-section">
        <h3>Transcript</h3>
        @if (data.turns.length === 0) {
          <div class="no-outcome">No transcript available for this call.</div>
        }
        @for (t of data.turns; track $index) {
          <div class="turn" [class.turn-user]="t.role === 'user'">
            <div class="turn-role">
              <mat-icon>{{ t.role === 'user' ? 'person' : 'support_agent' }}</mat-icon>
              {{ t.role === 'user' ? 'Caller' : 'Bot' }}
              <span class="turn-time">{{ formatTime(t.at) }}</span>
            </div>
            <div class="turn-text">{{ t.text }}</div>
          </div>
        }
      </section>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button mat-dialog-close>Close</button>
    </mat-dialog-actions>
  `,
  styles: [
    `
      .detail-title {
        margin-bottom: 0 !important;
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .session-id {
        font-family: 'JetBrains Mono', 'Consolas', monospace;
        color: #6b7280;
        font-size: 14px;
        font-weight: 400;
      }
      .detail-meta {
        font-size: 13px;
        color: #6b7280;
        font-weight: 400;
      }
      .detail-content {
        display: flex;
        flex-direction: column;
        gap: 16px;
      }
      h3 {
        margin: 0 0 8px;
        font-size: 14px;
        color: #6b7280;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .outcome-section,
      .transcript-section {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .appt-card {
        background: #f0fdf4;
        border: 1px solid #bbf7d0;
        border-radius: 8px;
        padding: 12px 14px;
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .appt-header {
        display: flex;
        align-items: center;
        gap: 8px;
        color: #166534;
        font-weight: 500;
        mat-icon {
          font-size: 18px;
          width: 18px;
          height: 18px;
        }
      }
      .appt-title {
        font-size: 15px;
      }
      .appt-body {
        color: rgba(0, 0, 0, 0.7);
        font-size: 13px;
        display: flex;
        flex-direction: column;
        gap: 2px;
      }
      .appt-actions {
        display: flex;
        justify-content: flex-end;
      }
      .muted {
        color: #6b7280;
      }
      .no-outcome {
        display: flex;
        align-items: center;
        gap: 8px;
        color: #6b7280;
        background: #f9fafb;
        border-radius: 8px;
        padding: 12px 14px;
        mat-icon {
          font-size: 18px;
          width: 18px;
          height: 18px;
        }
      }
      .turn {
        border-left: 3px solid #cbd5e1;
        padding: 6px 10px;
        background: #f8fafc;
        border-radius: 4px;
        margin-bottom: 6px;
      }
      .turn.turn-user {
        border-color: #2563eb;
        background: #eff6ff;
      }
      .turn-role {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.3px;
        margin-bottom: 2px;
        mat-icon {
          font-size: 14px;
          width: 14px;
          height: 14px;
        }
      }
      .turn.turn-user .turn-role {
        color: #1d4ed8;
      }
      .turn-time {
        margin-left: auto;
        font-family: 'JetBrains Mono', 'Consolas', monospace;
        text-transform: none;
        letter-spacing: 0;
      }
      .turn-text {
        white-space: pre-wrap;
        font-size: 14px;
        color: rgba(0, 0, 0, 0.85);
      }
    `,
  ],
})
export class CallDetailComponent {
  private readonly router = inject(Router);
  private readonly ref = inject(MatDialogRef<CallDetailComponent>);

  readonly formatDateTime = formatDateTime;
  readonly formatDuration = formatDuration;

  constructor(@Inject(MAT_DIALOG_DATA) readonly data: CallDetail) {}

  formatTime(iso: string): string {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
    });
  }

  viewInCalendar(a: Appointment): void {
    this.ref.close();
    this.router.navigate(['/calendar'], { queryParams: { date: a.date } });
  }
}
