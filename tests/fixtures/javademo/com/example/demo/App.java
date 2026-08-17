package com.example.demo;

import com.example.demo.EnglishGreeter;

public class App {
    private final Greeter greeter;

    public App(Greeter greeter) {
        this.greeter = greeter;
    }

    public void run() {
        String message = greeter.greet("World");
        System.out.println(message);
    }

    public static void main(String[] args) {
        App app = new App(new EnglishGreeter());
        app.run();
    }
}
