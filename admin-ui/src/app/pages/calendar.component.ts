import { CommonModule } from '@angular/common';
import { Component, Inject, OnInit, computed, inject, signal } from '@angular/core';
import {
  FormBuilder,
  FormGroup,
  ReactiveFormsModule,
  Validators,
} from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatCardModule } from '@angular/material/card';
import { MatDialog, MatDialogModule, MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTooltipModule } from '@angular/material/tooltip';

import {
  Appointment,
  ApiService,
  Service,
  Staff,
  WEEKDAYS,
} from '../api.service';

/** Local ISO date (YYYY-MM-DD) — never UTC, so 'today' feels local. */
function toIso(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function fromIso(s: string): Date {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function addDays(iso: string, delta: number): string {
  const d = fromIso(iso);
  d.setDate(d.getDate() + delta);
  return toIso(d);
}

function startOfWeek(iso: string): string {
  const d = fromIso(iso);
  d.setDate(d.getDate() - d.getDay()); // Sunday = 0
  return toIso(d);
}

interface DialogData {
  appointment: Appointment | null;
  staff: Staff[];
  services: Service[];
  defaultDate?: string;
}

@Component({
  selector: 'app-calendar',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatButtonToggleModule,
    MatCardModule,
    MatDialogModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatSnackBarModule,
    MatTooltipModule,
  ],
  template: `
    <div class="page">
      <div class="page-header">
        <h1>Calendar</h1>
        <button mat-flat-button color="primary" (click)="add()">
          <mat-icon>add</mat-icon> New appointment
        </button>
      </div>

      @if (error()) {
        <div class="error-banner">{{ error() }}</div>
      }

      <mat-card class="controls">
        <div class="controls-row">
          <button mat-icon-button (click)="shiftWeek(-1)" matTooltip="Previous week">
            <mat-icon>chevron_left</mat-icon>
          </button>
          <div class="week-label">
            {{ weekLabel() }}
          </div>
          <button mat-icon-button (click)="shiftWeek(1)" matTooltip="Next week">
            <mat-icon>chevron_right</mat-icon>
          </button>
          <button mat-stroked-button (click)="jumpToToday()">Today</button>
          <span class="spacer"></span>
          <span class="count">
            {{ appointments().length }}
            {{ appointments().length === 1 ? 'appointment' : 'appointments' }}
          </span>
        </div>
      </mat-card>

      @if (loading()) {
        <mat-spinner diameter="40"></mat-spinner>
      } @else {
        <div class="week-grid">
          @for (day of weekDays(); track day.iso) {
            <mat-card class="day-card" [class.today]="day.iso === today">
              <div class="day-header">
                <div>
                  <div class="day-name">{{ day.name }}</div>
                  <div class="day-date">{{ day.label }}</div>
                </div>
                <button
                  mat-icon-button
                  matTooltip="Add appointment"
                  (click)="add(day.iso)"
                >
                  <mat-icon>add</mat-icon>
                </button>
              </div>
              <div class="day-body">
                @for (a of dayAppointments(day.iso); track a.id) {
                  <div class="appt" (click)="edit(a)">
                    <div class="appt-time">
                      {{ a.start_time }}<span *ngIf="a.end_time">–{{ a.end_time }}</span>
                    </div>
                    <div class="appt-customer">{{ a.customer_name }}</div>
                    <div class="appt-detail">
                      {{ a.service }} with {{ a.stylist }}
                    </div>
                  </div>
                } @empty {
                  <div class="empty">No appointments</div>
                }
              </div>
            </mat-card>
          }
        </div>
      }
    </div>
  `,
  styles: [
    `
      .controls {
        margin-bottom: 16px;
        padding: 8px 16px;
      }
      .controls-row {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .week-label {
        font-weight: 500;
        min-width: 220px;
        text-align: center;
      }
      .spacer {
        flex: 1 1 auto;
      }
      .count {
        color: #6b7280;
        font-size: 13px;
      }
      .week-grid {
        display: grid;
        grid-template-columns: repeat(7, minmax(0, 1fr));
        gap: 8px;
      }
      @media (max-width: 1100px) {
        .week-grid {
          grid-template-columns: repeat(3, 1fr);
        }
      }
      @media (max-width: 700px) {
        .week-grid {
          grid-template-columns: 1fr;
        }
      }
      .day-card {
        padding: 0;
        display: flex;
        flex-direction: column;
        min-height: 260px;
      }
      .day-card.today {
        border-top: 3px solid #2563eb;
      }
      .day-header {
        padding: 8px 8px 4px 12px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px solid #f3f4f6;
      }
      .day-name {
        font-weight: 500;
      }
      .day-date {
        color: #6b7280;
        font-size: 12px;
      }
      .day-body {
        padding: 8px;
        flex: 1;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .appt {
        border-left: 3px solid #2563eb;
        background: #eff6ff;
        padding: 6px 8px;
        border-radius: 4px;
        cursor: pointer;
        transition: background 0.1s ease;
      }
      .appt:hover {
        background: #dbeafe;
      }
      .appt-time {
        font-weight: 500;
        font-size: 13px;
        color: #1d4ed8;
      }
      .appt-customer {
        font-size: 13px;
        margin-top: 2px;
      }
      .appt-detail {
        color: #6b7280;
        font-size: 12px;
      }
      .empty {
        color: #9ca3af;
        font-size: 12px;
        text-align: center;
        padding: 12px 0;
      }
    `,
  ],
})
export class CalendarComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly dialog = inject(MatDialog);
  private readonly snack = inject(MatSnackBar);

  readonly today = toIso(new Date());
  readonly weekStart = signal(startOfWeek(this.today));
  readonly appointments = signal<Appointment[]>([]);
  readonly staff = signal<Staff[]>([]);
  readonly services = signal<Service[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly weekDays = computed(() => {
    const start = this.weekStart();
    return Array.from({ length: 7 }, (_, i) => {
      const iso = addDays(start, i);
      const d = fromIso(iso);
      return {
        iso,
        name: WEEKDAYS[d.getDay()],
        label: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
      };
    });
  });

  readonly appointmentsByDay = computed(() => {
    const out: Record<string, Appointment[]> = {};
    for (const a of this.appointments()) {
      (out[a.date] ??= []).push(a);
    }
    for (const day in out) {
      out[day].sort((x, y) => x.start_time.localeCompare(y.start_time));
    }
    return out;
  });

  dayAppointments(iso: string): Appointment[] {
    return this.appointmentsByDay()[iso] ?? [];
  }

  readonly weekLabel = computed(() => {
    const start = fromIso(this.weekStart());
    const end = fromIso(addDays(this.weekStart(), 6));
    const fmt = (d: Date) =>
      d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const year = end.getFullYear();
    return `${fmt(start)} – ${fmt(end)}, ${year}`;
  });

  ngOnInit(): void {
    this.loadStaticData();
    this.refresh();
  }

  private loadStaticData(): void {
    this.api.listStaff().subscribe({ next: (s) => this.staff.set(s) });
    this.api.listServices().subscribe({ next: (s) => this.services.set(s) });
  }

  refresh(): void {
    this.loading.set(true);
    this.error.set(null);
    const start = this.weekStart();
    const end = addDays(start, 6);
    this.api.listAppointments(start, end).subscribe({
      next: (a) => {
        this.appointments.set(a);
        this.loading.set(false);
      },
      error: (e: Error) => {
        this.error.set(e.message);
        this.loading.set(false);
      },
    });
  }

  shiftWeek(direction: number): void {
    this.weekStart.set(addDays(this.weekStart(), direction * 7));
    this.refresh();
  }

  jumpToToday(): void {
    this.weekStart.set(startOfWeek(this.today));
    this.refresh();
  }

  add(date?: string): void {
    this.openDialog(null, date);
  }

  edit(a: Appointment): void {
    this.openDialog(a);
  }

  private openDialog(a: Appointment | null, defaultDate?: string): void {
    const ref = this.dialog.open(AppointmentDialogComponent, {
      data: {
        appointment: a,
        staff: this.staff(),
        services: this.services(),
        defaultDate,
      } satisfies DialogData,
      width: '520px',
      maxWidth: '92vw',
      autoFocus: 'first-tabbable',
    });
    ref.afterClosed().subscribe((r?: 'saved' | 'deleted') => {
      if (r === 'saved' || r === 'deleted') {
        this.refresh();
      }
    });
  }
}

@Component({
  selector: 'app-appointment-dialog',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatButtonModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatSelectModule,
  ],
  template: `
    <h2 mat-dialog-title>
      {{ data.appointment ? 'Edit appointment' : 'New appointment' }}
    </h2>
    <mat-dialog-content>
      <form [formGroup]="form" class="appt-form">
        <mat-form-field appearance="outline">
          <mat-label>Customer name</mat-label>
          <input matInput formControlName="customer_name" required />
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Phone</mat-label>
          <input matInput formControlName="customer_phone" required />
        </mat-form-field>

        <div class="form-row">
          <mat-form-field appearance="outline">
            <mat-label>Staff member</mat-label>
            <mat-select formControlName="stylist" required>
              @for (s of data.staff; track s.name) {
                <mat-option [value]="s.name">{{ s.name }}</mat-option>
              }
            </mat-select>
          </mat-form-field>
          <mat-form-field appearance="outline">
            <mat-label>Service</mat-label>
            <mat-select formControlName="service" required>
              @for (svc of data.services; track svc.name) {
                <mat-option [value]="svc.name">
                  {{ svc.name }} ({{ svc.duration_minutes }}m)
                </mat-option>
              }
            </mat-select>
          </mat-form-field>
        </div>

        <div class="form-row">
          <mat-form-field appearance="outline">
            <mat-label>Date</mat-label>
            <input matInput type="date" formControlName="date" required />
          </mat-form-field>
          <mat-form-field appearance="outline">
            <mat-label>Start time</mat-label>
            <input matInput type="time" formControlName="start_time" required />
          </mat-form-field>
        </div>

        @if (submitError()) {
          <div class="error-banner">{{ submitError() }}</div>
        }
      </form>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      @if (data.appointment) {
        <button mat-button color="warn" (click)="remove()" [disabled]="saving()">
          Cancel appointment
        </button>
      }
      <span class="grow"></span>
      <button mat-button mat-dialog-close>Close</button>
      <button
        mat-flat-button
        color="primary"
        [disabled]="form.invalid || saving()"
        (click)="save()"
      >
        {{ data.appointment ? 'Save changes' : 'Create' }}
      </button>
    </mat-dialog-actions>
  `,
  styles: [
    `
      .appt-form {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding-top: 4px;
      }
      .form-row {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
      }
      .grow {
        flex: 1 1 auto;
      }
    `,
  ],
})
export class AppointmentDialogComponent {
  private readonly api = inject(ApiService);
  private readonly snack = inject(MatSnackBar);

  readonly form: FormGroup;
  readonly saving = signal(false);
  readonly submitError = signal<string | null>(null);

  constructor(
    private readonly ref: MatDialogRef<AppointmentDialogComponent>,
    @Inject(MAT_DIALOG_DATA) readonly data: DialogData,
    fb: FormBuilder,
  ) {
    const a = data.appointment;
    this.form = fb.group({
      customer_name: fb.nonNullable.control(a?.customer_name ?? '', Validators.required),
      customer_phone: fb.nonNullable.control(a?.customer_phone ?? '', Validators.required),
      stylist: fb.nonNullable.control(a?.stylist ?? '', Validators.required),
      service: fb.nonNullable.control(a?.service ?? '', Validators.required),
      date: fb.nonNullable.control(a?.date ?? data.defaultDate ?? toIso(new Date()), Validators.required),
      start_time: fb.nonNullable.control(a?.start_time ?? '10:00', Validators.required),
    });
  }

  save(): void {
    if (this.form.invalid) return;
    const value = this.form.getRawValue();
    this.saving.set(true);
    this.submitError.set(null);
    const req$ = this.data.appointment
      ? this.api.updateAppointment(this.data.appointment.id, value)
      : this.api.createAppointment(value);
    req$.subscribe({
      next: () => {
        this.saving.set(false);
        this.snack.open('Appointment saved.', 'Dismiss', { duration: 3000 });
        this.ref.close('saved');
      },
      error: (e: Error) => {
        this.saving.set(false);
        this.submitError.set(e.message);
      },
    });
  }

  remove(): void {
    if (!this.data.appointment) return;
    if (!confirm('Cancel this appointment?')) return;
    this.saving.set(true);
    this.api.deleteAppointment(this.data.appointment.id).subscribe({
      next: () => {
        this.saving.set(false);
        this.snack.open('Appointment cancelled.', 'Dismiss', { duration: 3000 });
        this.ref.close('deleted');
      },
      error: (e: Error) => {
        this.saving.set(false);
        this.submitError.set(e.message);
      },
    });
  }
}
