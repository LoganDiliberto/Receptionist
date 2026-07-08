import { CommonModule } from '@angular/common';
import { Component, Inject, OnInit, inject, signal } from '@angular/core';
import {
  FormBuilder,
  FormGroup,
  ReactiveFormsModule,
  Validators,
} from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatCheckboxModule } from '@angular/material/checkbox';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialog, MatDialogModule, MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';

import { ApiService, Service, Staff, WEEKDAYS, Weekday } from '../api.service';

interface DialogData {
  staff: Staff | null;
  services: Service[];
}

interface DayFormValue {
  enabled: boolean;
  start: string;
  end: string;
}

@Component({
  selector: 'app-staff',
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
        <h1>Staff</h1>
        <button mat-flat-button color="primary" (click)="add()">
          <mat-icon>add</mat-icon> Add staff
        </button>
      </div>

      @if (error()) {
        <div class="error-banner">{{ error() }}</div>
      }

      @if (loading()) {
        <mat-spinner diameter="40"></mat-spinner>
      } @else if (staff().length === 0) {
        <mat-card>
          <mat-card-content>
            No staff yet. Click <b>Add staff</b> to get started.
          </mat-card-content>
        </mat-card>
      } @else {
        <mat-card class="table-card">
          <table mat-table [dataSource]="staff()" class="mat-elevation-z0">
            <ng-container matColumnDef="name">
              <th mat-header-cell *matHeaderCellDef>Name</th>
              <td mat-cell *matCellDef="let s">
                <div class="cell-name">{{ s.name }}</div>
              </td>
            </ng-container>

            <ng-container matColumnDef="services">
              <th mat-header-cell *matHeaderCellDef>Services</th>
              <td mat-cell *matCellDef="let s">
                <div class="chip-row">
                  @for (svc of s.services; track svc) {
                    <mat-chip>{{ svc | titlecase }}</mat-chip>
                  }
                </div>
              </td>
            </ng-container>

            <ng-container matColumnDef="schedule">
              <th mat-header-cell *matHeaderCellDef>Schedule</th>
              <td mat-cell *matCellDef="let s">
                <div class="schedule-cell">
                  @for (day of scheduleSummary(s); track day.day) {
                    <div>
                      <b>{{ day.day.slice(0, 3) }}</b> {{ day.text }}
                    </div>
                  } @empty {
                    <span class="muted">Not scheduled</span>
                  }
                </div>
              </td>
            </ng-container>

            <ng-container matColumnDef="actions">
              <th mat-header-cell *matHeaderCellDef class="actions-col"></th>
              <td mat-cell *matCellDef="let s" class="actions-col">
                <button mat-icon-button (click)="edit(s)" matTooltip="Edit">
                  <mat-icon>edit</mat-icon>
                </button>
                <button mat-icon-button (click)="remove(s)" matTooltip="Delete">
                  <mat-icon>delete</mat-icon>
                </button>
              </td>
            </ng-container>

            <tr mat-header-row *matHeaderRowDef="displayed"></tr>
            <tr mat-row *matRowDef="let row; columns: displayed"></tr>
          </table>
        </mat-card>
      }
    </div>
  `,
  styles: [
    `
      .table-card {
        padding: 0;
        overflow: hidden;
      }
      table {
        width: 100%;
      }
      .cell-name {
        font-weight: 500;
      }
      .schedule-cell {
        font-size: 12px;
        line-height: 1.4;
      }
      .muted {
        color: #9ca3af;
      }
      .actions-col {
        width: 96px;
        text-align: right;
      }
    `,
  ],
})
export class StaffComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly dialog = inject(MatDialog);
  private readonly snack = inject(MatSnackBar);

  readonly displayed = ['name', 'services', 'schedule', 'actions'];
  readonly staff = signal<Staff[]>([]);
  readonly services = signal<Service[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.listStaff().subscribe({
      next: (s) => {
        this.staff.set(s);
        this.loading.set(false);
      },
      error: (e: Error) => {
        this.error.set(e.message);
        this.loading.set(false);
      },
    });
    this.api.listServices().subscribe({
      next: (svc) => this.services.set(svc),
      error: () => {
        /* handled by the primary request */
      },
    });
  }

  scheduleSummary(s: Staff): { day: string; text: string }[] {
    const out: { day: string; text: string }[] = [];
    for (const day of WEEKDAYS) {
      const slot = s.schedule[day];
      if (slot && slot.start && slot.end) {
        out.push({ day, text: `${slot.start}–${slot.end}` });
      }
    }
    return out;
  }

  add(): void {
    this.openDialog(null);
  }

  edit(s: Staff): void {
    this.openDialog(s);
  }

  remove(s: Staff): void {
    if (!confirm(`Delete ${s.name}? This won't remove past appointments.`)) return;
    this.api.deleteStaff(s.name).subscribe({
      next: () => {
        this.snack.open(`Deleted ${s.name}`, 'Dismiss', { duration: 3000 });
        this.refresh();
      },
      error: (e: Error) => {
        this.snack.open(e.message, 'Dismiss', { duration: 5000 });
      },
    });
  }

  private openDialog(staff: Staff | null): void {
    const ref = this.dialog.open(StaffDialogComponent, {
      data: { staff, services: this.services() } satisfies DialogData,
      width: '640px',
      maxWidth: '92vw',
      autoFocus: 'first-tabbable',
    });
    ref.afterClosed().subscribe((result: 'saved' | undefined) => {
      if (result === 'saved') this.refresh();
    });
  }
}

@Component({
  selector: 'app-staff-dialog',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatDialogModule,
    MatButtonModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatCheckboxModule,
    MatIconModule,
  ],
  template: `
    <h2 mat-dialog-title>{{ data.staff ? 'Edit staff member' : 'Add staff member' }}</h2>
    <mat-dialog-content>
      <form [formGroup]="form" class="staff-form">
        <mat-form-field appearance="outline">
          <mat-label>Name</mat-label>
          <input matInput formControlName="name" required />
        </mat-form-field>

        <mat-form-field appearance="outline">
          <mat-label>Services offered</mat-label>
          <mat-select formControlName="services" multiple>
            @for (svc of data.services; track svc.name) {
              <mat-option [value]="svc.name.toLowerCase()">{{ svc.name }}</mat-option>
            }
          </mat-select>
          @if (data.services.length === 0) {
            <mat-hint>No services defined yet. Add some in the Services page first.</mat-hint>
          }
        </mat-form-field>

        <h3 class="section-title">Weekly schedule</h3>
        <div formArrayName="schedule" class="schedule-grid">
          @for (day of days; track day; let i = $index) {
            <div [formGroupName]="i" class="day-row">
              <mat-checkbox formControlName="enabled">{{ day }}</mat-checkbox>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>Start</mat-label>
                <input matInput type="time" formControlName="start" />
              </mat-form-field>
              <mat-form-field appearance="outline" subscriptSizing="dynamic">
                <mat-label>End</mat-label>
                <input matInput type="time" formControlName="end" />
              </mat-form-field>
            </div>
          }
        </div>

        @if (submitError()) {
          <div class="error-banner">{{ submitError() }}</div>
        }
      </form>
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button mat-dialog-close>Cancel</button>
      <button
        mat-flat-button
        color="primary"
        [disabled]="form.invalid || saving()"
        (click)="save()"
      >
        {{ data.staff ? 'Save changes' : 'Create' }}
      </button>
    </mat-dialog-actions>
  `,
  styles: [
    `
      .staff-form {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding-top: 8px;
      }
      .section-title {
        margin: 8px 0 4px;
        font-size: 14px;
        color: #6b7280;
        font-weight: 500;
      }
      .schedule-grid {
        display: flex;
        flex-direction: column;
        gap: 8px;
      }
      .day-row {
        display: grid;
        grid-template-columns: 130px 1fr 1fr;
        gap: 12px;
        align-items: center;
      }
      .error-banner {
        margin-top: 8px;
      }
    `,
  ],
})
export class StaffDialogComponent {
  private readonly api = inject(ApiService);
  readonly form: FormGroup;
  readonly days = WEEKDAYS;
  readonly saving = signal(false);
  readonly submitError = signal<string | null>(null);

  constructor(
    private readonly ref: MatDialogRef<StaffDialogComponent>,
    @Inject(MAT_DIALOG_DATA) readonly data: DialogData,
    fb: FormBuilder,
  ) {
    const staff = data.staff;
    this.form = fb.group({
      name: fb.nonNullable.control(staff?.name ?? '', {
        validators: [Validators.required, Validators.maxLength(80)],
      }),
      services: fb.nonNullable.control<string[]>(staff?.services ?? []),
      schedule: fb.array(this.days.map((day) => this.dayGroup(fb, staff, day))),
    });
  }

  private dayGroup(fb: FormBuilder, staff: Staff | null, day: Weekday): FormGroup {
    const slot = staff?.schedule[day];
    return fb.group({
      enabled: fb.nonNullable.control(!!slot),
      start: fb.nonNullable.control(slot?.start ?? '10:00'),
      end: fb.nonNullable.control(slot?.end ?? '17:00'),
    });
  }

  save(): void {
    if (this.form.invalid) return;
    const value = this.form.getRawValue();
    const schedule: Record<string, { start: string; end: string } | null> = {};
    (value.schedule as DayFormValue[]).forEach((slot, idx) => {
      const day = this.days[idx];
      schedule[day] = slot.enabled ? { start: slot.start, end: slot.end } : null;
    });
    const payload: Staff = {
      name: value.name,
      services: value.services,
      schedule,
    };
    this.saving.set(true);
    this.submitError.set(null);
    const req$ = this.data.staff
      ? this.api.updateStaff(this.data.staff.name, payload)
      : this.api.createStaff(payload);
    req$.subscribe({
      next: () => {
        this.saving.set(false);
        this.ref.close('saved');
      },
      error: (e: Error) => {
        this.saving.set(false);
        this.submitError.set(e.message);
      },
    });
  }
}
