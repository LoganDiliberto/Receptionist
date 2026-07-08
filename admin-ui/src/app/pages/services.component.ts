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
import { MatDialog, MatDialogModule, MatDialogRef, MAT_DIALOG_DATA } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTableModule } from '@angular/material/table';
import { MatTooltipModule } from '@angular/material/tooltip';

import { ApiService, Service } from '../api.service';

@Component({
  selector: 'app-services',
  standalone: true,
  imports: [
    CommonModule,
    MatButtonModule,
    MatCardModule,
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
        <h1>Services</h1>
        <button mat-flat-button color="primary" (click)="add()">
          <mat-icon>add</mat-icon> Add service
        </button>
      </div>

      @if (error()) {
        <div class="error-banner">{{ error() }}</div>
      }

      @if (loading()) {
        <mat-spinner diameter="40"></mat-spinner>
      } @else if (services().length === 0) {
        <mat-card>
          <mat-card-content>
            No services yet. Click <b>Add service</b> to get started.
          </mat-card-content>
        </mat-card>
      } @else {
        <mat-card class="table-card">
          <table mat-table [dataSource]="services()" class="mat-elevation-z0">
            <ng-container matColumnDef="name">
              <th mat-header-cell *matHeaderCellDef>Name</th>
              <td mat-cell *matCellDef="let s" class="cell-name">{{ s.name }}</td>
            </ng-container>

            <ng-container matColumnDef="duration">
              <th mat-header-cell *matHeaderCellDef>Duration</th>
              <td mat-cell *matCellDef="let s">{{ s.duration_minutes }} min</td>
            </ng-container>

            <ng-container matColumnDef="price">
              <th mat-header-cell *matHeaderCellDef>Price</th>
              <td mat-cell *matCellDef="let s">
                {{ s.price | currency: 'USD' : 'symbol' : '1.2-2' }}
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
      }
      table {
        width: 100%;
      }
      .cell-name {
        font-weight: 500;
      }
      .actions-col {
        width: 96px;
        text-align: right;
      }
    `,
  ],
})
export class ServicesComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly dialog = inject(MatDialog);
  private readonly snack = inject(MatSnackBar);

  readonly displayed = ['name', 'duration', 'price', 'actions'];
  readonly services = signal<Service[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api.listServices().subscribe({
      next: (s) => {
        this.services.set(s);
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

  edit(s: Service): void {
    this.openDialog(s);
  }

  remove(s: Service): void {
    if (!confirm(`Delete service "${s.name}"?`)) return;
    this.api.deleteService(s.name).subscribe({
      next: () => {
        this.snack.open(`Deleted ${s.name}`, 'Dismiss', { duration: 3000 });
        this.refresh();
      },
      error: (e: Error) => this.snack.open(e.message, 'Dismiss', { duration: 5000 }),
    });
  }

  private openDialog(service: Service | null): void {
    const ref = this.dialog.open(ServiceDialogComponent, {
      data: service,
      width: '480px',
      maxWidth: '92vw',
      autoFocus: 'first-tabbable',
    });
    ref.afterClosed().subscribe((r: 'saved' | undefined) => {
      if (r === 'saved') this.refresh();
    });
  }
}

@Component({
  selector: 'app-service-dialog',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatButtonModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
  ],
  template: `
    <h2 mat-dialog-title>{{ data ? 'Edit service' : 'Add service' }}</h2>
    <mat-dialog-content>
      <form [formGroup]="form" class="svc-form">
        <mat-form-field appearance="outline">
          <mat-label>Name</mat-label>
          <input matInput formControlName="name" required />
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Duration (minutes)</mat-label>
          <input
            matInput
            type="number"
            min="30"
            step="30"
            formControlName="duration_minutes"
            required
          />
          <mat-hint>Multiple of 30 minutes.</mat-hint>
        </mat-form-field>
        <mat-form-field appearance="outline">
          <mat-label>Price (USD)</mat-label>
          <input matInput type="number" min="0" step="0.01" formControlName="price" required />
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
      .svc-form {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding-top: 4px;
      }
    `,
  ],
})
export class ServiceDialogComponent {
  private readonly api = inject(ApiService);
  readonly form: FormGroup;
  readonly saving = signal(false);
  readonly submitError = signal<string | null>(null);

  constructor(
    private readonly ref: MatDialogRef<ServiceDialogComponent>,
    @Inject(MAT_DIALOG_DATA) readonly data: Service | null,
    fb: FormBuilder,
  ) {
    this.form = fb.group({
      name: fb.nonNullable.control(data?.name ?? '', {
        validators: [Validators.required, Validators.maxLength(80)],
      }),
      duration_minutes: fb.nonNullable.control(data?.duration_minutes ?? 30, {
        validators: [Validators.required, Validators.min(30)],
      }),
      price: fb.nonNullable.control(data?.price ?? 0, {
        validators: [Validators.required, Validators.min(0)],
      }),
    });
  }

  save(): void {
    if (this.form.invalid) return;
    const value = this.form.getRawValue();
    const payload: Service = {
      name: value.name,
      duration_minutes: Number(value.duration_minutes),
      price: Number(value.price),
    };
    this.saving.set(true);
    this.submitError.set(null);
    const req$ = this.data
      ? this.api.updateService(this.data.name, payload)
      : this.api.createService(payload);
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
