package com.redhat.lightwell.service;

import java.math.BigDecimal;
import java.util.List;
import java.util.Optional;

import com.redhat.lightwell.model.Account;
import com.redhat.lightwell.model.AccountType;
import com.redhat.lightwell.model.Customer;
import com.redhat.lightwell.model.Transaction;
import com.redhat.lightwell.model.dto.TransactionResponse;
import com.redhat.lightwell.model.dto.TransferRequest;
import com.redhat.lightwell.repository.AccountRepository;
import com.redhat.lightwell.repository.TransactionRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.ArgumentCaptor;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Verifies that a customer can transfer exactly the full available balance
 * from one account to a different account: the source ends at zero, the
 * target receives the same amount, and no funds disappear or appear.
 *
 * <p>This boundary (amount == balance, whereas the funds check in
 * {@code TransactionService.transfer} uses {@code compareTo(...) < 0}) is not
 * covered by {@code TransactionServiceTest}, which only exercises a partial
 * transfer of 200.00 out of a 1000.00 balance and an overdrawn withdrawal.</p>
 */
@ExtendWith(MockitoExtension.class)
class TransactionServiceFullBalanceTransferTest {

    @Mock
    private AccountRepository accountRepository;

    @Mock
    private TransactionRepository transactionRepository;

    @Mock
    private AuditService auditService;

    @InjectMocks
    private TransactionService transactionService;

    private Customer customer;
    private Account source;
    private Account target;

    @BeforeEach
    void setUp() {
        customer = new Customer("John", "Doe", "john@example.com", "555-0101");
        source = new Account("ACC-000001", AccountType.CHECKING, new BigDecimal("1000.00"), customer);
        target = new Account("ACC-000002", AccountType.SAVINGS, new BigDecimal("250.00"), customer);
    }

    @Test
    void shouldTransferExactFullAvailableBalanceToDifferentAccount() {
        // Exactly the full available balance of the source account.
        BigDecimal fullBalance = new BigDecimal("1000.00");
        BigDecimal sourceBalanceBefore = source.getBalance();
        BigDecimal targetBalanceBefore = target.getBalance();

        when(accountRepository.findById(1L)).thenReturn(Optional.of(source));
        when(accountRepository.findById(2L)).thenReturn(Optional.of(target));
        when(accountRepository.save(any(Account.class))).thenAnswer(invocation -> invocation.getArgument(0));
        when(transactionRepository.save(any(Transaction.class))).thenAnswer(invocation -> invocation.getArgument(0));

        TransferRequest request = new TransferRequest(1L, 2L, fullBalance, "Full balance transfer");

        // The transfer must complete successfully; an unexpected exception
        // (e.g. InsufficientFundsException from a guard that wrongly treats
        // amount == balance as an overdraw) becomes an assertion failure.
        TransactionResponse response = assertDoesNotThrow(
                () -> transactionService.transfer(request),
                "Transferring exactly the full available balance must complete and must not be "
                        + "rejected as insufficient funds");

        assertThat(response).isNotNull();

        // Exactly the full available balance is the amount moved.
        assertThat(response.getAmount()).isEqualByComparingTo(fullBalance);

        // The returned transaction is the source leg: the source is drained to exactly zero.
        assertThat(response.getBalanceAfter()).isEqualByComparingTo(new BigDecimal("0.00"));

        // Both updated accounts are written back through the repository.
        ArgumentCaptor<Account> accountCaptor = ArgumentCaptor.forClass(Account.class);
        verify(accountRepository, times(2)).save(accountCaptor.capture());
        List<Account> savedAccounts = accountCaptor.getAllValues();

        // The service saves the source first, then the target.
        Account savedSource = savedAccounts.get(0);
        Account savedTarget = savedAccounts.get(1);

        // The source ends at exactly zero (never negative, never short-changed).
        assertThat(savedSource.getBalance()).isEqualByComparingTo(new BigDecimal("0.00"));
        assertThat(source.getBalance()).isEqualByComparingTo(new BigDecimal("0.00"));

        // The target receives exactly the transferred amount: 250.00 + 1000.00.
        assertThat(savedTarget.getBalance()).isEqualByComparingTo(new BigDecimal("1250.00"));
        assertThat(target.getBalance()).isEqualByComparingTo(new BigDecimal("1250.00"));

        // Fund conservation: the total across both accounts is unchanged
        // (no funds disappear or appear).
        BigDecimal totalBefore = sourceBalanceBefore.add(targetBalanceBefore);
        BigDecimal totalAfter = savedSource.getBalance().add(savedTarget.getBalance());
        assertThat(totalAfter).isEqualByComparingTo(totalBefore);
        assertThat(totalAfter).isEqualByComparingTo(new BigDecimal("1250.00"));

        // The amount debited from the source equals the amount credited to the target.
        BigDecimal debited = sourceBalanceBefore.subtract(savedSource.getBalance());
        BigDecimal credited = savedTarget.getBalance().subtract(targetBalanceBefore);
        assertThat(debited).isEqualByComparingTo(fullBalance);
        assertThat(credited).isEqualByComparingTo(fullBalance);
    }
}
