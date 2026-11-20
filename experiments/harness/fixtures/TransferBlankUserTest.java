package com.projectlightpath.fixture;

import static io.restassured.RestAssured.given;
import static org.hamcrest.CoreMatchers.is;

import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import org.junit.jupiter.api.Test;

@QuarkusTest
class TransferBlankUserTest {
    @Test
    void rejectsBlankUser() {
        given()
                .contentType(ContentType.JSON)
                .body("{\"requestReference\":\"flpath-5050-blank-user\",\"user\":\"\"}")
                .when()
                .post("/transfers")
                .then()
                .statusCode(200)
                .body("requestReference", is("flpath-5050-blank-user"))
                .body("accepted", is(false));
    }
}
