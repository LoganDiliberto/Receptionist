import { CommonModule } from '@angular/common';
import {
  Component,
  Inject,
  OnDestroy,
  OnInit,
  inject,
  signal,
} from '@angular/core';
import {
  FormBuilder,
  FormGroup,
  ReactiveFormsModule,
  Validators,
} from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import {
  MAT_DIALOG_DATA,
  MatDialog,
  MatDialogModule,
  MatDialogRef,
} from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTableModule } from '@angular/material/table';
import { MatTabsModule } from '@angular/material/tabs';
import { MatTooltipModule } from '@angular/material/tooltip';
import { Subject, debounceTime, takeUntil } from 'rxjs';

import {
  ApiService,
  Appointment,
  Client,
  ClientPayload,
  Gender,
} from '../api.service';

const GENDER_OPTIONS: Array<{ value: Gender; label: string }> = [
  { value: 'female', label: 'Female' },
  { value: 'male', label: 'Male' },
  { value: 'nonbinary', label: 'Non-binary' },
  { value: 'unspecified', label: 'Prefer not to say' },
];

@Component({
  selector: 'app-clients',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatButtonModule,
    MatCardModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatProgressSpinnerModule,
    MatSelectModule,
    MatSnackBarModule,
    MatTableModule,
    MatTabsModule,
    MatTooltipModule,
  ],
  template: `
    <div class="page">
      <div class="page-header">
        <h1>Clients</h1>
        <button mat-flat-button color="primary" (click)="add()">
          <mat-icon>person_add</mat-icon> Add client
        </button>
      </div>

      <mat-form-field appearance="outline" class="search">
        <mat-icon matPrefix>search</mat-icon>
        <mat-label>Search by name or phone</mat-label>
        <input
          matInput
          type="search"
          [value]="query()"
          (input)="onQueryInput($any($event.target).value)"
          placeholder="Ada, 555…"
        />
        @if (query()) {
          <button
            mat-icon-button
            matSuffix
            aria-label="Clear"
            (click)="clearQuery()"
          >
            <mat-icon>close</mat-icon>
          </button>
        }
      </mat-form-field>

      @if (error()) {
        <div class="error-banner">{{ error() }}</div>
      }

      @if (loading()) {
        <mat-spinner diameter="40"></mat-spinner>
      } @else if (clients().length === 0) {
        <mat-card>
          <mat-card-content>
            @if (query()) {
              No clients match "<b>{{ query() }}</b>".
            } @else {
              No clients yet. The bot will add them automatically after their
              first booking, or you can create one manually.
            }
          </mat-card-content>
        </mat-card>
      } @else {
        <mat-card class="table-card">
          <table mat-table [dataSource]="clients()" class="mat-elevation-z0">
            <ng-container matColumnDef="name">
              <th mat-header-cell *matHeaderCellDef>Name</th>
              <td mat-cell *matCellDef="let c" class="cell-name">
                @if (c.first_name || c.last_name) {
                  {{ c.first_name }} {{ c.last_name }}
                } @else {
                  <span class="muted">(no name on file)</span>
                }
              </td>
            </ng-container>

            <ng-container matColumnDef="phone">
              <th mat-header-cell *matHeaderCellDef>Phone</th>
              <td mat-cell *matCellDef="let c">{{ c.phone_formatted }}</td>
            </ng-container>

            <ng-container matColumnDef="email">
              <th mat-header-cell *matHeaderCellDef>Email</th>
              <td mat-cell *matCellDef="let c">
                {{ c.email || '' }}
              </td>
            </ng-container>

            <ng-container matColumnDef="updated">
              <th mat-header-cell *matHeaderCellDef>Last updated</th>
              <td mat-cell *matCellDef="let c">{{ formatDate(c.updated_at) }}</td>
            </ng-container>

            <ng-container matColumnDef="actions">
              <th mat-header-cell *matHeaderCellDef class="actions-col"></th>
              <td mat-cell *matCellDef="let c" class="actions-col">
                <button
                  mat-icon-button
                  (click)="openHistory(c)"
                  matTooltip="View appointments"
                >
                  <mat-icon>history</mat-icon>
                </button>
                <button mat-icon-button (click)="edit(c)" matTooltip="Edit">
                  <mat-icon>edit</mat-icon>
                </button>
                <button mat-icon-button (click)="remove(c)" matTooltip="Delete">
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
      .search {
        width: 100%;
        max-width: 480px;
        margin-bottom: 12px;
      }
      .table-card {
        padding: 0;
      }
      table {
        width: 100%;
      }
      .cell-name {
        font-weight: 500;
      }
      .muted {
        color: #6b7280;
        font-weight: 400;
      }
      .actions-col {
        width: 144px;
        text-align: right;
      }
    `,
  ],
})
export class ClientsComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);
  private readonly dialog = inject(MatDialog);
  private readonly snack = inject(MatSnackBar);
  private readonly destroy$ = new Subject<void>();
  private readonly queryChanges$ = new Subject<string>();

  readonly displayed = ['name', 'phone', 'email', 'updated', 'actions'];
  readonly clients = signal<Client[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly query = signal<string>('');

  ngOnInit(): void {
    // Debounce keystrokes so we don't hammer /api/clients on every char.
    this.queryChanges$
      .pipe(debounceTime(200), takeUntil(this.destroy$))
      .subscribe((q) => this.refresh(q));
    this.refresh('');
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  onQueryInput(value: string): void {
    this.query.set(value);
    this.queryChanges$.next(value);
  }

  clearQuery(): void {
    this.query.set('');
    this.refresh('');
  }

  refresh(query: string): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.listClients(query || undefined).subscribe({
      next: (list) => {
        this.clients.set(list);
        this.loading.set(false);
      },
      error: (e: Error) => {
        this.error.set(e.message);
        this.loading.set(false);
      },
    });
  }

  add(): void {
    this.openDialog(null);
  }

  edit(c: Client): void {
    this.openDialog(c);
  }

  remove(c: Client): void {
    const label = displayName(c);
    if (!confirm(`Delete client "${label}"? Past appointments will be kept but detached.`)) return;
    this.api.deleteClient(c.id).subscribe({
      next: () => {
        this.snack.open(`Deleted ${label}`, 'Dismiss', { duration: 3000 });
        this.refresh(this.query());
      },
      error: (e: Error) => this.snack.open(e.message, 'Dismiss', { duration: 5000 }),
    });
  }

  openHistory(c: Client): void {
    this.dialog.open(ClientHistoryDialogComponent, {
      data: c,
      width: '640px',
      maxWidth: '92vw',
    });
  }

  formatDate(iso: string): string {
    // Compact human-readable date; falls back to the raw ISO on parse error.
    try {
      return new Date(iso).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return iso;
    }
  }

  private openDialog(c: Client | null): void {
    const ref = this.dialog.open(ClientDialogComponent, {
      data: c,
      width: '520px',
      maxWidth: '92vw',
      autoFocus: 'first-tabbable',
    });
    ref.afterClosed().subscribe((r: 'saved' | undefined) => {
      if (r === 'saved') this.refresh(this.query());
    });
  }
}

function displayName(c: Pick<Client, 'first_name' | 'last_name' | 'phone_formatted'>): string {
  const full = `${c.first_name ?? ''} ${c.last_name ?? ''}`.trim();
  return full || c.phone_formatted;
}

@Component({
  selector: 'app-client-dialog',
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
    <h2 mat-dialog-title>{{ data ? 'Edit client' : 'Add client' }}</h2>
    <mat-dialog-content>
      <form [formGroup]="form" class="client-form">
        <div class="row">
          <mat-form-field appearance="outline">
            <mat-label>First name</mat-label>
            <input matInput formControlName="first_name" />
          </mat-form-field>
          <mat-form-field appearance="outline">
            <mat-label>Last name</mat-label>
            <input matInput formControlName="last_name" />
          </mat-form-field>
        </div>
        <mat-form-field appearance="outline">
          <mat-label>Phone</mat-label>
          <input matInput formControlName="phone" required placeholder="(203) 555-0100" />
          <mat-hint>US 10-digit number. Any format accepted.</mat-hint>
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Email</mat-label>
          <input matInput formControlName="email" type="email" />
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Gender</mat-label>
          <mat-select formControlName="gender">
            <mat-option [value]="null">Not set</mat-option>
            @for (opt of genderOptions; track opt.value) {
              <mat-option [value]="opt.value">{{ opt.label }}</mat-option>
            }
          </mat-select>
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Notes</mat-label>
          <textarea
            matInput
            formControlName="notes"
            rows="3"
            placeholder="Prefers early appointments, allergic to X, etc."
          ></textarea>
        </mat-form-field>
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
        {{ data ? 'Save changes' : 'Create' }}
      </button>
    </mat-dialog-actions>
  `,
  styles: [
    `
      .client-form {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding-top: 4px;
      }
      .row {
        display: flex;
        gap: 8px;
      }
      .row mat-form-field {
        flex: 1;
      }
    `,
  ],
})
export class ClientDialogComponent {
  private readonly api = inject(ApiService);
  readonly form: FormGroup;
  readonly saving = signal(false);
  readonly submitError = signal<string | null>(null);
  readonly genderOptions = GENDER_OPTIONS;

  constructor(
    private readonly ref: MatDialogRef<ClientDialogComponent>,
    @Inject(MAT_DIALOG_DATA) readonly data: Client | null,
    fb: FormBuilder,
  ) {
    this.form = fb.group({
      first_name: fb.nonNullable.control(data?.first_name ?? ''),
      last_name: fb.nonNullable.control(data?.last_name ?? ''),
      phone: fb.nonNullable.control(data?.phone ?? '', {
        validators: [Validators.required, Validators.minLength(10)],
      }),
      email: fb.control<string | null>(data?.email ?? null),
      gender: fb.control<Gender | null>((data?.gender ?? null) as Gender | null),
      notes: fb.control<string | null>(data?.notes ?? null),
    });
  }

  save(): void {
    if (this.form.invalid) return;
    const value = this.form.getRawValue();
    const payload: ClientPayload = {
      first_name: value.first_name ?? '',
      last_name: value.last_name ?? '',
      phone: value.phone,
      email: value.email || null,
      gender: value.gender ?? null,
      notes: value.notes || null,
    };
    this.saving.set(true);
    this.submitError.set(null);
    const req$ = this.data
      ? this.api.updateClient(this.data.id, payload)
      : this.api.createClient(payload);
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

@Component({
  selector: 'app-client-history-dialog',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatDialogModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatTableModule,
    MatTabsModule,
  ],
  template: `
    <h2 mat-dialog-title>
      Appointments for {{ nameFor() }}
      <div class="dialog-sub">{{ data.phone_formatted }}</div>
    </h2>
    <mat-dialog-content>
      @if (loading()) {
        <mat-spinner diameter="32"></mat-spinner>
      } @else {
        <mat-tab-group>
          <mat-tab [label]="'Upcoming (' + upcoming().length + ')'">
            @if (upcoming().length === 0) {
              <p class="empty">No upcoming appointments.</p>
            } @else {
              <ul class="appt-list">
                @for (a of upcoming(); track a.id) {
                  <li>
                    <strong>{{ a.service }}</strong>
                    with {{ a.stylist }} — {{ a.date }} at {{ a.start_time }}
                  </li>
                }
              </ul>
            }
          </mat-tab>
          <mat-tab [label]="'Past (' + past().length + ')'">
            @if (past().length === 0) {
              <p class="empty">No past appointments on record.</p>
            } @else {
              <ul class="appt-list">
                @for (a of past(); track a.id) {
                  <li>
                    <strong>{{ a.service }}</strong>
                    with {{ a.stylist }} — {{ a.date }} at {{ a.start_time }}
                  </li>
                }
              </ul>
            }
          </mat-tab>
        </mat-tab-group>
      }
    </mat-dialog-content>
    <mat-dialog-actions align="end">
      <button mat-button mat-dialog-close>Close</button>
    </mat-dialog-actions>
  `,
  styles: [
    `
      .dialog-sub {
        font-size: 12px;
        color: #6b7280;
        font-weight: 400;
      }
      .appt-list {
        list-style: none;
        padding: 0;
        margin: 12px 0 0 0;
      }
      .appt-list li {
        padding: 8px 0;
        border-bottom: 1px solid #f3f4f6;
      }
      .appt-list li:last-child {
        border-bottom: none;
      }
      .empty {
        color: #6b7280;
        padding: 12px 0;
      }
    `,
  ],
})
export class ClientHistoryDialogComponent implements OnInit {
  private readonly api = inject(ApiService);
  readonly loading = signal(true);
  readonly upcoming = signal<Appointment[]>([]);
  readonly past = signal<Appointment[]>([]);

  constructor(@Inject(MAT_DIALOG_DATA) readonly data: Client) {}

  ngOnInit(): void {
    this.api.getClientAppointments(this.data.id).subscribe({
      next: (all) => {
        const today = new Date().toISOString().slice(0, 10);
        this.upcoming.set(all.filter((a) => a.date >= today));
        this.past.set(all.filter((a) => a.date < today));
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  nameFor(): string {
    return displayName(this.data);
  }
}
