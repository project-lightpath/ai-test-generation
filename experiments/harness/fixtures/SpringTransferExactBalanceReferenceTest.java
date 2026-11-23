package com.redhat.lightwell.service;

import java.math.BigDecimal;
import java.util.Optional;

import com.redhat.lightwell.model.Account;
import com.redhat.lightwell.model.AccountType;
import com.redhat.lightwell.model.Customer;
import com.redhat.lightwell.model.Transaction;
import com.redhat.lightwell.model.dto.TransactionResponse;
import com.redhat.lightwell.model.dto.TransferRequest;
import com.redhat.lightwell.repository.AccountRepository;
import com.redhat.lightwell.repository.TransactionRepository;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

class SpringTransferExactBalanceReferenceTest {

    @Test
    void transferringEntireAvailableBalanceKeepsFundsBalanced() {
        AccountRepository accounts = mock(AccountRepository.class);
        TransactionRepository transactions = mock(TransactionRepository.class);
        AuditService audit = mock(AuditService.class);
        TransactionService service = new TransactionService(accounts, transactions, audit);

        Customer customer = new Customer("Test", "User", "test@example.invalid", "555-0100");
        Account source = new Account("SOURCE-001", AccountType.CHECKING,
                new BigDecimal("1000.00"), customer);
        Account target = new Account("TARGET-001", AccountType.SAVINGS,
                new BigDecimal("500.00"), customer);
        when(accounts.findById(1L)).thenReturn(Optional.of(source));
        when(accounts.findById(2L)).thenReturn(Optional.of(target));
        when(transactions.save(any(Transaction.class)))
                .thenAnswer(invocation -> invocation.getArgument(0));

        TransferRequest request = new TransferRequest(1L, 2L,
                new BigDecimal("1000.00"), "Full balance transfer");
        TransactionResponse response = assertDoesNotThrow(() -> service.transfer(request));

        assertThat(response.getAmount()).isEqualByComparingTo("1000.00");
        assertThat(response.getBalanceAfter()).isEqualByComparingTo("0.00");
        assertThat(source.getBalance()).isEqualByComparingTo("0.00");
        assertThat(target.getBalance()).isEqualByComparingTo("1500.00");
    }
}
